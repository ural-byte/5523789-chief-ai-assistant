import asyncio
import functools
import os
import time
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.config import Settings
from app.db import make_sessions
from app.domain_actions import Approval, Task
from app.models import DeletionCleanup, History, Invocation, Job, Operation, Outbox, now
from app.privacy import guard_operation, owner_lock
from app.queue import claim
from app.supervisor import supervise
from tests.test_storage import operation


def stalled_child(mode, config):
    """A real spawned executor, including code that cannot yield to asyncio timers."""
    sessions = make_sessions(config.database_url)
    with sessions.begin() as session:
        job = claim(session, Job, 42, exclude_kinds={"data_cleanup"})
    if job is None:
        time.sleep(120)
        return
    if mode == "crash":
        os._exit(7)
    if mode == "pg_lock":
        try:
            with sessions.begin() as session:
                guard_operation(session, job.operation_id, (job.id, job.lease_token))
                session.execute(text("UPDATE worker_lock_fixture SET value=1"))
        except Exception:
            pass
        time.sleep(120)
        return
    if mode == "owner_idle":
        with sessions.begin() as session:
            owner_lock(session, 42)
            time.sleep(120)
        return
    from app.pricing import Usage
    from app.providers import Generation, ToolCall
    from app.runtime import Runtime
    from app.tools import Registry, ToolResult
    from tests.test_providers_runtime import Arguments

    class Provider:
        def estimate_request_budget(self, *args):
            return 1

        async def generate(self, *args):
            if mode == "ai":
                await asyncio.Event().wait()
            return Generation(
                "", [ToolCall("frozen", "search_memory", {"query": "x"})], {}, Usage(), "test"
            )

    class Tool:
        arguments = Arguments
        description = "fixture"

        async def prepare(self, *args):
            if mode == "sync_tool":
                time.sleep(120)
            await asyncio.Event().wait()

        def apply(self, *args):
            return ToolResult(status="ok")

    registry = Registry()
    registry.register("search_memory", Tool())
    asyncio.run(
        Runtime(sessions, Provider(), registry).run(job.operation_id, (job.id, job.lease_token))
    )
    time.sleep(120)


async def wait_for(sessions, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sessions() as session:
            if predicate(session):
                return
        await asyncio.sleep(0.1)
    pytest.fail("independent process condition did not complete within bounded fixture time")


@pytest.mark.parametrize("mode", ["ai", "async_tool", "sync_tool", "pg_lock", "crash"])
async def test_real_process_watchdog_kills_stall_and_preserves_absolute_deadline(
    sessions, tmp_path, mode
):
    opid = operation(sessions)
    cfg = Settings(
        database_url=str(sessions.kw["bind"].url.render_as_string(hide_password=False)),
        allowed_telegram_user_id=42,
        file_directory=tmp_path,
    )
    with sessions.begin() as session:
        op = session.get(Operation, opid)
        op.deadline_at = now() + timedelta(seconds=3)
        deadline = op.deadline_at
        session.add(Job(key="frozen", operation_id=opid, kind="agent", payload={}))
        session.execute(text("CREATE TABLE worker_lock_fixture(value integer)"))
        session.execute(text("INSERT INTO worker_lock_fixture VALUES(0)"))
    blocker = sessions()
    if mode == "pg_lock":
        blocker.execute(text("UPDATE worker_lock_fixture SET value=2"))
    stop = asyncio.Event()
    task = asyncio.create_task(
        supervise(cfg, functools.partial(stalled_child, mode), stop, cadence=0.2)
    )
    try:
        await wait_for(
            sessions, lambda s: s.get(Operation, opid).error_reason == "deadline", timeout=14
        )
        with sessions() as session:
            op = session.get(Operation, opid)
            assert op.deadline_at == deadline and op.terminal_revision == 1
            assert session.scalar(select(Job)).status == "cancelled"
            rows = session.scalars(select(Outbox)).all()
            assert len(rows) == 1 and rows[0].terminal_revision == 1
            assert all(not h.message.get("content") for h in session.scalars(select(History)))
        await asyncio.sleep(0.4)
        with sessions() as session:
            assert len(session.scalars(select(Outbox)).all()) == 1
    finally:
        stop.set()
        await task
        blocker.rollback()
        blocker.close()
        with sessions.begin() as session:
            session.execute(text("DROP TABLE worker_lock_fixture"))


@pytest.mark.parametrize("mode", ["sync_tool", "owner_idle"])
async def test_responsive_parent_scheduler_and_confirmed_cleanup_before_frozen_child_deadline(
    sessions, tmp_path, mode
):
    opid = operation(sessions)
    cleanup_op = operation(sessions)
    filename = f"{cleanup_op}.pdf"
    (tmp_path / filename).write_bytes(b"private dummy")
    with sessions.begin() as session:
        op = session.get(Operation, opid)
        op.deadline_at = now() + timedelta(seconds=25)
        session.add(Job(key="frozen", operation_id=opid, kind="agent", payload={}))
        inv = Invocation(
            operation_id=cleanup_op, call_id="approved", name="prepare_data_deletion", arguments={}
        )
        task_inv = Invocation(operation_id=opid, call_id="task", name="create_task", arguments={})
        session.add_all([inv, task_inv])
        session.flush()
        approval = Approval(
            invocation_id=inv.id,
            operation_id=cleanup_op,
            owner_id=42,
            chat_id=42,
            action_kind="data_deletion",
            payload={"counts": {"documents": 1}},
            status="executing",
            approved_at=now(),
            expires_at=now() + timedelta(hours=24),
        )
        session.add(approval)
        session.flush()
        aid = approval.id
        session.add(DeletionCleanup(approval_id=aid, paths=[filename], remaining=[filename]))
        session.add(
            Job(
                key="confirmed-cleanup",
                available_at=now() + timedelta(seconds=3),
                operation_id=cleanup_op,
                kind="data_cleanup",
                payload={"approval_id": str(aid)},
            )
        )
        session.add(
            Task(
                invocation_id=task_inv.id,
                operation_id=opid,
                owner_id=42,
                chat_id=42,
                text="due dummy",
                deadline=now() + timedelta(seconds=3),
                source_timezone="Europe/Moscow",
                reference_at=now(),
            )
        )
    cfg = Settings(
        database_url=sessions.kw["bind"].url.render_as_string(hide_password=False),
        allowed_telegram_user_id=42,
        file_directory=tmp_path,
    )
    stop = asyncio.Event()
    service = asyncio.create_task(
        supervise(cfg, functools.partial(stalled_child, mode), stop, cadence=0.2)
    )
    try:
        await wait_for(
            sessions,
            lambda s: s.scalar(select(Job).where(Job.key == "frozen")).status == "running",
            timeout=3,
        )
        with sessions() as session:
            assert session.get(Approval, aid).status == "executing"
        await wait_for(sessions, lambda s: s.get(Approval, aid).status == "executed", timeout=18)
        await wait_for(
            sessions,
            lambda s: s.scalar(select(Outbox.id).where(Outbox.key.like("task:%:reminder")))
            is not None,
            timeout=12,
        )
        assert not (tmp_path / filename).exists()
        with sessions() as session:
            op = session.get(Operation, opid)
            assert now() < op.deadline_at and op.error_reason is None
            assert session.scalar(select(Job).where(Job.key == "frozen")).status == "running"
        # Delivery records a real mock Telegram receipt, independently of the frozen child.
        import httpx
        from fastapi.testclient import TestClient
        from pydantic import SecretStr

        from app import api
        from app.workers import deliver_once

        cfg.service_token = SecretStr("test")
        from unittest.mock import patch

        with (
            patch.object(api, "settings", lambda: cfg),
            patch.object(api, "session_factory", lambda: sessions),
        ):
            client = TestClient(api.app)

            class Backend:
                async def post(self, path, **kwargs):
                    return client.post(path, headers={"Authorization": "Bearer test"}, **kwargs)

            async with httpx.AsyncClient(
                base_url="https://telegram.invalid/",
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
                ),
            ) as bot:
                await deliver_once(Backend(), bot)
                await deliver_once(Backend(), bot)
        with sessions() as session:
            reminder = session.scalar(select(Outbox).where(Outbox.key.like("task:%:reminder")))
            assert reminder.status == "done" and reminder.acknowledged_at
            assert session.get(Operation, opid).final_delivery_ack_at is None
    finally:
        stop.set()
        await service

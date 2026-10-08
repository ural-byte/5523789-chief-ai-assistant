import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models import AICall, History, Job, Operation, Outbox, now
from app.privacy import guard_operation
from app.providers import YandexProvider
from app.queue import LeaseLost, acknowledge, claim, enqueue_text
from app.runtime import Runtime
from app.terminal import ExecutionExpired, InvalidFinal, delivery_sweep, expire_operations, nonblank
from app.tools import Registry
from tests.test_providers_runtime import config
from tests.test_storage import operation


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("value", ["  \n\t", "\u200b\u200d\ufeff", "\u2060"])
async def test_billable_blank_final_is_single_explicit_error(sessions, protocol, value):
    opid = operation(sessions)
    cfg = config(protocol)

    def respond(request):
        content = value if protocol == "native" else json.dumps({"type": "final", "text": value})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    provider = YandexProvider(cfg, sessions, httpx.MockTransport(respond))
    await Runtime(sessions, provider, Registry(), protocol).run(opid)
    with sessions() as session:
        op = session.get(Operation, opid)
        assert op.status == "error" and op.error_reason == "invalid_final"
        rows = session.scalars(select(Outbox)).all()
        assert len(rows) == 1 and nonblank(rows[0].payload["text"])
        assert rows[0].terminal_revision == op.terminal_revision == 1
        event = session.scalar(select(AICall))
        assert event.status == "error" and event.error_code == "invalid_final"
        assert event.input_tokens == 100 and event.output_tokens == 50 and event.cost is not None
        assert not session.scalar(select(History).where(History.message["content"].astext == value))


@pytest.mark.parametrize("value", ["", " \n ", "\u200b"])
def test_queue_refuses_blank_and_discards_whitespace_only_split_chunks(sessions, value):
    opid = operation(sessions)
    with pytest.raises(InvalidFinal), sessions.begin() as session:
        enqueue_text(session, session.get(Operation, opid), value)
    with sessions.begin() as session:
        enqueue_text(session, session.get(Operation, opid), "x" + " " * 9000 + "y")
    with sessions() as session:
        assert all(nonblank(row.payload["text"]) for row in session.scalars(select(Outbox)))


def queue(sessions, kind="final", text="Valid"):
    opid = operation(sessions)
    with sessions.begin() as session:
        op = session.get(Operation, opid)
        op.status = "done"
        enqueue_text(session, op, text, purpose=kind)
    return opid


@pytest.mark.parametrize("failure", ["permanent", "payload"])
def test_permanent_single_revision_recovery_then_failure_no_recursion(sessions, failure):
    opid = queue(sessions)
    with sessions.begin() as session:
        original = claim(session, Outbox, 42)
    with sessions.begin() as session:
        acknowledge(session, Outbox, original.id, original.lease_token, "rejected", failure)
    with sessions.begin() as session:
        fallback = claim(session, Outbox, 42)
        assert fallback.id != original.id and fallback.terminal_revision == 1
    with sessions.begin() as session:
        acknowledge(session, Outbox, fallback.id, fallback.lease_token, "rejected", "permanent")
    with sessions.begin() as session:
        delivery_sweep(session, 42)
        assert claim(session, Outbox, 42) is None
    with sessions() as session:
        op = session.get(Operation, opid)
        assert op.delivery_state == "failed" and op.final_delivery_ack_at is None
        assert len(session.scalars(select(Outbox)).all()) == 2


@pytest.mark.parametrize("failure", ["rate_limit", "server", "network"])
def test_transient_max4_and_durable_budget(sessions, failure):
    opid = queue(sessions)
    deadline = None
    for attempt in range(4):
        with sessions.begin() as session:
            original = claim(session, Outbox, 42)
        with sessions.begin() as session:
            row = session.get(Outbox, original.id)
            deadline = deadline or row.delivery_deadline_at
            assert row.delivery_deadline_at == deadline
            acknowledge(
                session,
                Outbox,
                row.id,
                original.lease_token,
                "transient",
                failure,
                2 if failure == "rate_limit" else None,
            )
            if attempt < 3:
                assert row.status == "pending" and row.available_at > now()
                row.available_at = now() - timedelta(seconds=1)
    with sessions() as session:
        assert session.get(Outbox, original.id).attempts == 4
        assert session.get(Operation, opid).terminal_revision == 1


def test_legacy_done_blank8_repair_without_agent_rerun_actual_ack(sessions):
    opid = operation(sessions)
    with sessions.begin() as session:
        op = session.get(Operation, opid)
        op.status = "done"
        session.add(
            Outbox(
                key="legacy",
                operation_id=opid,
                kind="sendMessage",
                payload={"text": "  "},
                attempts=8,
            )
        )
        session.add(
            Job(key="legacyjob", operation_id=opid, kind="agent", payload={}, status="done")
        )
    with sessions.begin() as session:
        delivery_sweep(session, 42)
        delivery_sweep(session, 42)
        recovery = claim(session, Outbox, 42)
    with sessions.begin() as session:
        acknowledge(session, Outbox, recovery.id, recovery.lease_token)
    with sessions() as session:
        op = session.get(Operation, opid)
        assert op.status == "error" and op.delivery_state == "delivered"
        assert op.final_delivery_ack_at
        assert session.scalar(select(Job)).status == "done"
        assert len(session.scalars(select(Outbox)).all()) == 2


def test_old_revision_acks_and_independent_reminder_cannot_complete_current_final(sessions):
    opid = queue(sessions)
    with sessions.begin() as session:
        first = claim(session, Outbox, 42)
        acknowledge(session, Outbox, first.id, first.lease_token, "bad", "permanent")
        op = session.get(Operation, opid)
        enqueue_text(session, op, "reminder", purpose="notification", key_prefix="reminder")
    with sessions.begin() as session:
        fallback = claim(session, Outbox, 42)
        acknowledge(session, Outbox, fallback.id, fallback.lease_token)
    with sessions() as session:
        op = session.get(Operation, opid)
        stamp = op.final_delivery_ack_at
        revision = op.terminal_revision
    with sessions.begin() as session:
        reminder = claim(session, Outbox, 42)
        acknowledge(session, Outbox, reminder.id, reminder.lease_token, "bad", "permanent")
    with sessions() as session:
        op = session.get(Operation, opid)
        assert op.final_delivery_ack_at == stamp and op.terminal_revision == revision


def test_before_commit_deadline_blocks_late_write_and_repair_never_renews_budget(sessions):
    import time

    opid = operation(sessions)
    with sessions.begin() as session:
        session.get(Operation, opid).deadline_at = now() + timedelta(milliseconds=100)
    with pytest.raises(ExecutionExpired), sessions.begin() as session:
        guard_operation(session, opid)
        session.add(History(operation_id=opid, owner_id=42, message={"content": "late"}))
        time.sleep(0.15)
    with sessions.begin() as session:
        expire_operations(session, 42)
        original = session.get(Operation, opid).deadline_at
        expire_operations(session, 42)
        assert session.get(Operation, opid).deadline_at == original
    with sessions() as session:
        assert not session.scalar(select(History))
        assert session.get(Operation, opid).error_reason == "deadline"
    with pytest.raises(LeaseLost), sessions.begin() as session:
        guard_operation(session, opid)


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
async def test_transport_actual_response_classification_and_bounded_recovery(
    sessions, monkeypatch, status
):
    from fastapi.testclient import TestClient

    from app import api
    from app.config import Settings
    from app.workers import deliver_once

    cfg = Settings(service_token="test", allowed_telegram_user_id=42)
    monkeypatch.setattr(api, "settings", lambda: cfg)
    monkeypatch.setattr(api, "session_factory", lambda: sessions)
    client = TestClient(api.app)

    class Backend:
        async def post(self, path, **kwargs):
            return client.post(path, headers={"Authorization": "Bearer test"}, **kwargs)

    opid = queue(sessions)
    accepted = []

    def response(request):
        accepted.append(request)
        return httpx.Response(
            status,
            json={
                "ok": False,
                "error_code": status,
                "description": "private text must never enter metrics",
                "parameters": {"retry_after": 2},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://telegram.invalid/", transport=httpx.MockTransport(response)
    ) as bot:
        await deliver_once(Backend(), bot)
    with sessions() as session:
        op = session.get(Operation, opid)
        rows = session.scalars(select(Outbox).order_by(Outbox.available_at)).all()
        original = next(r for r in rows if r.terminal_revision == 0)
        assert len(accepted) == 1 and original.acknowledged_at is None
        assert "private" not in str(original.error_code)
        if status in {400, 401, 403}:
            assert original.status == "failed" and op.terminal_revision == 1
        else:
            assert original.status == "pending" and op.terminal_revision == 0
            assert original.failure_class == ("rate_limit" if status == 429 else "server")


def test_running_expired_fourth_attempt_cannot_be_claimed_for_fifth(sessions):
    queue(sessions)
    with sessions.begin() as session:
        row = claim(session, Outbox, 42)
        row.attempts = 4
        row.lease_until = now() - timedelta(seconds=1)
        original_id = row.id
    with sessions.begin() as session:
        claimed = claim(session, Outbox, 42)
        assert claimed.id != original_id
        assert session.get(Outbox, original_id).attempts == 4


def test_multipart_aggregate_only_actual_all_current_revision_ack(sessions):
    opid = operation(sessions)
    with sessions.begin() as session:
        op = session.get(Operation, opid)
        op.status = "done"
        enqueue_text(session, op, "😀" * 5000)
    for index in range(3):
        with sessions.begin() as session:
            row = claim(session, Outbox, 42)
            acknowledge(session, Outbox, row.id, row.lease_token)
        with sessions() as session:
            op = session.get(Operation, opid)
            assert bool(op.final_delivery_ack_at) == (index == 2)
    with sessions() as session:
        assert session.get(Operation, opid).final_delivery_ack_at == max(
            r.acknowledged_at for r in session.scalars(select(Outbox))
        )


async def test_recovery_after_committed_deletion_preserves_real_result_without_undo_claim(sessions):
    from app.domain_actions import Approval
    from app.domain_memory import MemoryEntry
    from tests.test_actions import callback
    from tests.test_memory_resolution import fixture_pair, prepare

    ids, _, _ = await fixture_pair(sessions)
    aid, _ = prepare(sessions)
    await callback(sessions, aid)
    with sessions.begin() as session:
        final = session.scalars(
            select(Outbox).where(Outbox.kind == "sendMessage", Outbox.status == "pending")
        ).all()[-1]
        op = session.get(Operation, final.operation_id)
        final.status = "running"
        final.lease_token = __import__("uuid").uuid4()
        final.lease_until = now() + timedelta(seconds=120)
        acknowledge(session, Outbox, final.id, final.lease_token, "rejected", "permanent")
        session.flush()
        recovery = session.scalar(
            select(Outbox).where(
                Outbox.operation_id == op.id, Outbox.terminal_revision == op.terminal_revision
            )
        )
        assert recovery and "Проверьте результат" in recovery.payload["text"]
        assert (
            "Удаление не" not in recovery.payload["text"]
            and "отмен" not in recovery.payload["text"]
        )
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"
        assert not session.get(MemoryEntry, ids[0]) and session.get(MemoryEntry, ids[1])

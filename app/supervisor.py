"""An OS watchdog keeps maintenance responsive when execution stops yielding."""

import asyncio
import logging
import multiprocessing
from contextlib import suppress

from sqlalchemy import select

from app.db import make_sessions
from app.domain_actions import scheduler_once
from app.models import Job, Operation, now
from app.queue import heartbeat
from app.terminal import delivery_sweep, expire_operations

log = logging.getLogger("supervisor")


def execute_child(config):
    from app.bootstrap import install
    from app.providers import YandexProvider
    from app.workers import agent_loop

    sessions = make_sessions(config.database_url)
    provider = YandexProvider(config, sessions)
    install(sessions, provider, config)
    try:
        asyncio.run(agent_loop(sessions, provider, config))
    finally:
        sessions.kw["bind"].dispose()


def overdue_execution(sessions, owner):
    with sessions() as session:
        return (
            session.scalar(
                select(Job.id)
                .join(Operation)
                .where(
                    Operation.owner_id == owner,
                    Operation.deadline_at <= now(),
                    Job.kind != "data_cleanup",
                    Job.status == "running",
                )
                .limit(1)
            )
            is not None
        )


def repair(sessions, owner):
    with sessions.begin() as session:
        expire_operations(session, owner)
        for job in session.scalars(
            select(Job)
            .join(Operation)
            .where(
                Operation.owner_id == owner,
                Operation.deadline_at <= now(),
                Operation.status.in_({"done", "error", "cancelled"}),
                Job.kind != "data_cleanup",
                Job.status == "running",
            )
        ):
            job.status, job.lease_token, job.lease_until = "done", None, None
        delivery_sweep(session, owner)
        heartbeat(session, "background")


async def terminate(process):
    if process.is_alive():
        process.terminate()
        await asyncio.to_thread(process.join, 2)
    if process.is_alive():
        process.kill()
        await asyncio.to_thread(process.join, 2)
    if process.is_alive():
        raise RuntimeError("executor_did_not_exit")
    process.close()


async def supervise(config, child_target=execute_child, stop=None, cadence=2):
    # spawn never inherits the parent's live PostgreSQL connections or asyncio loop.
    context = multiprocessing.get_context("spawn")
    sessions = make_sessions(config.database_url)
    maintenance_sessions = make_sessions(config.database_url)
    from app.bootstrap import install
    from app.workers import background_once

    install(maintenance_sessions, config=config)
    process = None

    async def maintenance():
        while stop is None or not stop.is_set():
            try:
                # Cleanup is already approved. Its lane bypasses an active agent job.
                await asyncio.to_thread(
                    lambda: asyncio.run(
                        background_once(maintenance_sessions, None, config, maintenance=True)
                    )
                )
            except Exception as exc:
                log.error("maintenance_failed code=%s", type(exc).__name__)
            await asyncio.sleep(0.25)

    async def scheduler():
        while stop is None or not stop.is_set():
            try:
                await asyncio.to_thread(
                    scheduler_once, maintenance_sessions, config.allowed_telegram_user_id
                )
            except Exception as exc:
                log.error("scheduler_failed code=%s", type(exc).__name__)
            await asyncio.sleep(5)

    tasks = [asyncio.create_task(maintenance()), asyncio.create_task(scheduler())]
    try:
        while stop is None or not stop.is_set():
            try:
                expired = await asyncio.to_thread(
                    overdue_execution, sessions, config.allowed_telegram_user_id
                )
                if process and (expired or not process.is_alive()):
                    await terminate(process)
                    process = None
                await asyncio.to_thread(repair, sessions, config.allowed_telegram_user_id)
                if process is None:
                    process = context.Process(target=child_target, args=(config,), daemon=True)
                    process.start()
            except Exception as exc:
                log.error("watchdog_failed code=%s", type(exc).__name__)
            await asyncio.sleep(cadence)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        if process:
            await terminate(process)
        for factory in (sessions, maintenance_sessions):
            factory.kw["bind"].dispose()

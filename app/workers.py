import asyncio
import logging
import sys
from contextlib import suppress

import httpx
from sqlalchemy import select

from app.config import settings
from app.models import Job, Operation
from app.queue import LeaseLost, acknowledge, claim, enqueue_text, heartbeat, renew
from app.runtime import Runtime
from app.terminal import ExecutionExpired, nonblank
from app.tools import registry

log = logging.getLogger("workers")


async def lease_renewal(sessions, item, token):
    while True:
        await asyncio.sleep(15)
        with sessions.begin() as session:
            renew(session, Job, item, token)
            heartbeat(session, "background")


async def background_once(sessions, provider, config, maintenance=False, supervised=False):
    with sessions.begin() as session:
        heartbeat(session, "background")
        job = claim(
            session,
            Job,
            config.allowed_telegram_user_id,
            include_kinds={"data_cleanup"} if maintenance else None,
            exclude_kinds={"data_cleanup"} if supervised else None,
            maintenance=maintenance,
        )
    if job is None:
        return False
    token = job.lease_token
    renewal = asyncio.create_task(lease_renewal(sessions, job.id, token))
    try:
        if job.kind == "agent":
            await Runtime(sessions, provider, registry, config.tool_protocol).run(
                job.operation_id, (job.id, token)
            )
        elif job.kind == "data_prepare":
            from app.data_controls import deletion_scope, prepare_deletion
            from app.models import Invocation
            from app.privacy import guard_operation

            with sessions.begin() as session:
                op = guard_operation(session, job.operation_id, (job.id, token))
                inv = session.scalar(
                    select(Invocation).where(
                        Invocation.operation_id == op.id, Invocation.call_id == "data-command"
                    )
                )
                if inv is None:
                    inv = Invocation(
                        operation_id=op.id,
                        call_id="data-command",
                        name="prepare_data_deletion",
                        arguments={},
                    )
                    session.add(inv)
                    session.flush()
                result = prepare_deletion(
                    session,
                    op,
                    inv.id,
                    deletion_scope(job.payload.get("message", {}).get("text", "")),
                    config.file_directory,
                )
                inv.result = result.model_dump()
                op.status = "done"
                enqueue_text(session, op, result.user_message, result.buttons)
        elif job.kind == "callback" and registry.callback:
            await registry.callback(job, (job.id, token))
        elif job.kind in registry.jobs:
            await registry.jobs[job.kind](job, (job.id, token))
        else:
            with sessions.begin() as session:
                from app.queue import require_lease

                require_lease(session, Job, job.id, token)
                op = session.get(Operation, job.operation_id)
                op.status = "error"
                enqueue_text(session, op, "Этот сценарий ещё недоступен в текущей версии.")
        with sessions.begin() as session:
            acknowledge(session, Job, job.id, token)
            from app.latency import finish_operation

            finish_operation(session, session.get(Operation, job.operation_id))
    except ExecutionExpired:
        from app.privacy import owner_lock
        from app.terminal import terminal_error

        with sessions.begin() as session:
            op = session.get(Operation, job.operation_id)
            owner_lock(session, op.owner_id)
            terminal_error(session, op, "deadline")
    except LeaseLost:
        log.warning("lease_lost job=%s", job.id)
        with sessions.begin() as session:
            from app.privacy import owner_lock

            op = session.get(Operation, job.operation_id)
            owner_lock(session, op.owner_id)
            row = session.get(Job, job.id, with_for_update=True)
            if row.status == "running" and row.lease_token == token:
                row.status, row.lease_token, row.lease_until = "cancelled", None, None
                op.status = "cancelled"
                from app.latency import finish_operation

                finish_operation(session, op)
    except Exception as exc:
        # Exception messages may contain provider requests or credentials; log only the class.
        log.error("job_failed job=%s code=%s", job.id, type(exc).__name__)
        with sessions.begin() as session:
            with suppress(LeaseLost):
                acknowledge(session, Job, job.id, token, type(exc).__name__)
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError, LeaseLost):
            await renewal
    return True


async def background():
    from app.supervisor import supervise

    await supervise(settings())


async def agent_loop(sessions, provider, config):
    while True:
        try:
            worked = await background_once(sessions, provider, config, supervised=True)
            if not worked:
                await asyncio.sleep(1)
        except Exception as exc:
            log.error("background_failed code=%s", type(exc).__name__)
            await asyncio.sleep(5)


async def transport_loop(name, action, interval):
    import random

    failures = 0
    while True:
        try:
            await action()
            failures = 0
            if interval:
                await asyncio.sleep(interval)
        except Exception as exc:
            failures += 1
            log.error("telegram_%s_failed code=%s", name, type(exc).__name__)
            await asyncio.sleep(min(5, 2 ** min(failures - 1, 3) + random.uniform(0, 0.2)))


async def poll_once(backend, bot):
    checkpoint = await backend.get("/internal/checkpoint")
    checkpoint.raise_for_status()
    response = await bot.post(
        "getUpdates",
        json={
            "offset": checkpoint.json()["value"],
            "timeout": 25,
            "allowed_updates": ["message", "callback_query"],
        },
        timeout=httpx.Timeout(35, connect=5),
    )
    response.raise_for_status()
    updates = response.json()
    if not updates.get("ok"):
        raise RuntimeError("telegram_get_updates")
    for update in updates["result"]:
        accepted = await backend.post("/internal/updates", json=update)
        accepted.raise_for_status()
        committed = await backend.post(
            "/internal/checkpoint", json={"value": update["update_id"] + 1}
        )
        committed.raise_for_status()


async def deliver_once(backend, bot):
    import time

    claim_started = time.monotonic()
    claimed = await backend.post("/internal/outbox/claim")
    claimed.raise_for_status()
    item = claimed.json()
    if not item:
        return
    # A revoked lease may have been cancelled after claim. Check immediately
    # before network IO; after an accepted external send its duplicate semantics
    # remain the documented Telegram at-least-once boundary.
    renewed = await backend.post(
        f"/internal/outbox/{item['id']}/renew", json={"lease_token": item["lease_token"]}
    )
    if renewed.status_code == 409:
        return
    renewed.raise_for_status()
    error, failure_class, retry_after, started = None, None, None, time.monotonic()
    try:
        budget = min(10, item.get("remaining_seconds", 10) - (time.monotonic() - claim_started))
        if budget <= 0 or (
            item["kind"] == "sendMessage" and not nonblank(item["payload"].get("text"))
        ):
            error, failure_class = "telegram_payload", "payload"
        else:
            sent = await bot.post(
                item["kind"],
                json=item["payload"],
                timeout=httpx.Timeout(budget, connect=min(5, budget)),
            )
            try:
                body = sent.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            code = body.get("error_code", sent.status_code)
            if not isinstance(code, int):
                code = sent.status_code
            if sent.is_error or not body.get("ok"):
                error = "telegram_rejected"
                failure_class = (
                    "permanent"
                    if code in {400, 401, 403}
                    else "rate_limit"
                    if code == 429
                    else "server"
                )
                candidate = body.get("parameters", {}).get("retry_after")
                if (
                    code == 429
                    and isinstance(candidate, (int, float))
                    and not isinstance(candidate, bool)
                ):
                    retry_after = max(0, min(86400, candidate))
    except (httpx.HTTPError, ValueError):
        error, failure_class = "telegram_send_failed", "network"
    ack = await backend.post(
        f"/internal/outbox/{item['id']}/ack",
        json={
            "lease_token": item["lease_token"],
            "error": error,
            "failure_class": failure_class,
            "retry_after": retry_after,
            "send_latency_ms": round((time.monotonic() - started) * 1000),
        },
    )
    if ack.status_code != 409:
        ack.raise_for_status()


async def activity_once(backend, bot):
    response = await backend.post("/internal/activity/claim")
    response.raise_for_status()

    async def typing(item):
        try:
            sent = await bot.post(
                "sendChatAction",
                json={"chat_id": item["chat_id"], "action": "typing"},
                timeout=httpx.Timeout(3.5, connect=3),
            )
            sent.raise_for_status()
            if sent.json().get("ok"):
                ack = await backend.post(f"/internal/activity/{item['operation_id']}/ack")
                ack.raise_for_status()
        except (httpx.HTTPError, ValueError):
            log.warning("telegram_activity_failed operation=%s", item["operation_id"])

    await asyncio.gather(*(typing(item) for item in response.json()))


async def telegram():
    config = settings()
    headers = {"Authorization": "Bearer " + config.service_token.get_secret_value()}
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10, connect=5), headers=headers, base_url=config.backend_url
    ) as backend:

        async def beat():
            response = await backend.post("/internal/heartbeat/telegram")
            response.raise_for_status()

        if not config.telegram_bot_token.get_secret_value():
            await transport_loop("heartbeat", beat, 5)
            return
        url = "https://api.telegram.org/bot" + config.telegram_bot_token.get_secret_value() + "/"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), base_url=url) as bot:

            async def poll():
                await poll_once(backend, bot)

            async def deliver():
                await deliver_once(backend, bot)

            async def activity():
                await activity_once(backend, bot)

            await asyncio.gather(
                transport_loop("poll", poll, 0),
                transport_loop("delivery", deliver, 0.25),
                transport_loop("activity", activity, 0.25),
                transport_loop("heartbeat", beat, 5),
            )


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(background() if sys.argv[1] == "background" else telegram())


if __name__ == "__main__":
    main()

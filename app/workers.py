import asyncio
import logging
import sys
from contextlib import suppress

import httpx

from app.config import settings
from app.db import session_factory
from app.models import Job, Operation
from app.providers import YandexProvider
from app.queue import LeaseLost, acknowledge, claim, enqueue_text, heartbeat, renew
from app.runtime import Runtime
from app.tools import registry

log = logging.getLogger("workers")


async def lease_renewal(sessions, item, token):
    while True:
        await asyncio.sleep(15)
        with sessions.begin() as session:
            renew(session, Job, item, token)
            heartbeat(session, "background")


async def background_once(sessions, provider, config):
    with sessions.begin() as session:
        heartbeat(session, "background")
        job = claim(session, Job, config.allowed_telegram_user_id)
    if job is None:
        return False
    token = job.lease_token
    renewal = asyncio.create_task(lease_renewal(sessions, job.id, token))
    try:
        if job.kind == "agent":
            await Runtime(sessions, provider, registry, config.tool_protocol).run(
                job.operation_id, (job.id, token)
            )
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
    except LeaseLost:
        log.warning("lease_lost job=%s", job.id)
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
    config, sessions = settings(), session_factory()
    provider = YandexProvider(config, sessions)
    while True:
        try:
            worked = await background_once(sessions, provider, config)
            if not worked:
                await asyncio.sleep(1)
        except Exception as exc:
            log.error("background_failed code=%s", type(exc).__name__)
            await asyncio.sleep(5)


async def telegram():
    config = settings()
    headers = {"Authorization": "Bearer " + config.service_token.get_secret_value()}
    # Disable httpx logging: Bot API tokens form part of its URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    async with httpx.AsyncClient(
        timeout=40, headers=headers, base_url=config.backend_url
    ) as backend:
        while True:
            try:
                beat = await backend.post("/internal/heartbeat/telegram")
                beat.raise_for_status()
                if not config.telegram_bot_token.get_secret_value():
                    await asyncio.sleep(5)
                    continue
                checkpoint = await backend.get("/internal/checkpoint")
                checkpoint.raise_for_status()
                offset = checkpoint.json()["value"]
                url = "https://api.telegram.org/bot" + config.telegram_bot_token.get_secret_value()
                async with httpx.AsyncClient(timeout=35) as bot:
                    response = await bot.post(
                        url + "/getUpdates",
                        json={
                            "offset": offset,
                            "timeout": 3,
                            "allowed_updates": ["message", "callback_query"],
                        },
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
                    for _ in range(20):
                        claimed = await backend.post("/internal/outbox/claim")
                        claimed.raise_for_status()
                        item = claimed.json()
                        if not item:
                            break
                        error = None
                        try:
                            sent = await bot.post(url + "/" + item["kind"], json=item["payload"])
                            sent.raise_for_status()
                            if not sent.json().get("ok"):
                                error = "telegram_rejected"
                        except (httpx.HTTPError, ValueError):
                            error = "telegram_send_failed"
                        ack = await backend.post(
                            f"/internal/outbox/{item['id']}/ack",
                            json={"lease_token": item["lease_token"], "error": error},
                        )
                        ack.raise_for_status()
            except Exception as exc:
                log.error("telegram_failed code=%s", type(exc).__name__)
                await asyncio.sleep(5)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(background() if sys.argv[1] == "background" else telegram())


if __name__ == "__main__":
    main()

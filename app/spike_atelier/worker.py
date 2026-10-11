import asyncio
import logging

import httpx

from app.db import make_sessions
from app.domain_actions import scheduler_once
from app.spike_atelier.config import SpikeSettings
from app.spike_atelier.service import SpikeService
from app.spike_atelier.transport import deliver_once


async def main():
    config = SpikeSettings()
    if not config.telegram_bot_token.get_secret_value():
        raise ValueError("separate spike bot token is required")
    service = SpikeService(make_sessions(config.database_url), None, config)
    service.require_binding_gate()
    # Never log HTTP request URLs: Telegram includes the secret in the path.
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True
    async with httpx.AsyncClient(
        base_url="https://api.telegram.org/bot"
        + config.telegram_bot_token.get_secret_value()
        + "/",
        timeout=httpx.Timeout(10, connect=5),
    ) as bot:
        while True:
            try:
                await asyncio.to_thread(scheduler_once, service.sessions, config.owner_id)
                callback = await service.callbacks_once()
                delivery = await deliver_once(service, bot)
            except Exception as exc:
                logging.getLogger("spike").error("transport_failed code=%s", type(exc).__name__)
                await asyncio.sleep(2)
                continue
            if not (callback or delivery):
                await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())

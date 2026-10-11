from uuid import UUID

import httpx

from app.models import Outbox
from app.queue import acknowledge, claim, require_lease
from app.spike_atelier.receipts import SpikeReceipt


async def deliver_once(service, bot):
    """ACK создаётся только transport после успешного Telegram API response."""
    service.require_binding_gate()
    with service.sessions.begin() as session:
        row = claim(session, Outbox, service.config.owner_id)
    if not row:
        return False
    try:
        response = await bot.post(row.kind, json=row.payload)
        response.raise_for_status()
        receipt = response.json()
        result = receipt.get("result")
        if receipt.get("ok") is not True or (
            row.kind == "sendMessage"
            and (
                not isinstance(result, dict)
                or type(result.get("message_id")) is not int
                or result.get("chat", {}).get("id") != row.payload.get("chat_id")
            )
        ):
            raise ValueError("invalid_telegram_receipt")
        with service.sessions.begin() as session:
            require_lease(session, Outbox, row.id, row.lease_token)
            if row.kind == "sendMessage":
                buttons = row.payload.get("reply_markup", {}).get("inline_keyboard", [])
                raw = buttons[0][0].get("callback_data", "") if buttons and buttons[0] else ""
                approval_id = UUID(raw.split(":")[1]) if raw.startswith("a:") else None
                session.add(
                    SpikeReceipt(
                        outbox_id=row.id,
                        owner_id=service.config.owner_id,
                        chat_id=row.payload["chat_id"],
                        message_id=result["message_id"],
                        approval_id=approval_id,
                    )
                )
            acknowledge(session, Outbox, row.id, row.lease_token)
    except (httpx.HTTPError, ValueError):
        with service.sessions.begin() as session:
            acknowledge(session, Outbox, row.id, row.lease_token, error="telegram_delivery_failed")
    return True

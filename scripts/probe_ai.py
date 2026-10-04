"""Безопасная проверка tool roundtrip и обоих embedding endpoints."""

import asyncio
import json
import sys
import uuid

from app.config import settings
from app.db import session_factory
from app.models import Operation
from app.providers import ProviderError, YandexProvider, usage_totals
from app.runtime import result_message


async def main():
    config = settings()
    if not config.ai_api_key.get_secret_value() or not config.ai_folder_id:
        print(json.dumps({"status": "pending", "reason": "AI environment is not configured"}))
        return 2
    sessions = session_factory()
    with sessions.begin() as session:
        op = Operation(
            owner_id=config.allowed_telegram_user_id,
            chat_id=config.allowed_telegram_user_id,
            timezone=config.timezone,
            scenario="probe",
        )
        session.add(op)
        session.flush()
        operation_id = op.id
    provider = YandexProvider(config, sessions)
    nonce = str(uuid.uuid4())
    tools = [
        {
            "type": "function",
            "function": {
                "name": "safe_echo",
                "description": "Безопасно повторить указанное значение",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    messages = [{"role": "user", "content": f"Вызови safe_echo со значением {nonce}."}]
    try:
        first = await provider.generate(operation_id, messages, tools)
        if len(first.tools) != 1 or first.tools[0].name != "safe_echo":
            raise ProviderError("probe_tool_selection_failed")
        call = first.tools[0]
        if call.arguments != {"value": nonce}:
            raise ProviderError("probe_arguments_failed")
        messages.extend(
            [first.message, result_message(call.id, {"value": nonce}, config.tool_protocol)]
        )
        messages.append({"role": "user", "content": "Верни значение из результата инструмента."})
        second = await provider.generate(operation_id, messages, [])
        if second.tools or nonce not in second.text:
            raise ProviderError("probe_roundtrip_failed")
        for purpose in ("doc", "query"):
            await provider.embed(operation_id, "Проверка эмбеддингов", purpose)
        status = "passed"
        code = None
    except ProviderError as exc:
        status, code = "failed", str(exc)
    with sessions.begin() as session:
        session.get(Operation, operation_id).status = "done" if status == "passed" else "error"
        report = {
            "status": status,
            "error_code": code,
            "protocol": config.tool_protocol,
            "operation_id": str(operation_id),
            "usage": usage_totals(session, operation_id),
        }
    print(json.dumps(report, ensure_ascii=False))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

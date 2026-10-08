"""Bounded Telegram file transfer; credentials never appear in exception messages."""

import logging
import re
import tempfile

import httpx
from sqlalchemy import select

from app.domain_memory import MAX_BYTES, Document
from app.models import Operation
from app.privacy import guard_operation
from app.queue import enqueue_text


async def download_document(sessions, config, job, lease):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    metadata = job.payload.get("message", {}).get("document", {})

    def fail(message):
        with sessions.begin() as session:
            guard_operation(session, job.operation_id, lease)
            op = session.get(Operation, job.operation_id)
            op.status = "error"
            enqueue_text(session, op, message)

    with sessions() as session:
        if session.scalar(select(Document.id).where(Document.operation_id == job.operation_id)):
            return
    if metadata.get("file_size", 0) > MAX_BYTES:
        fail("PDF превышает 10 МБ.")
        return
    name = metadata.get("file_name", "document.pdf")
    if not name.casefold().endswith(".pdf"):
        fail("Поддерживаются только текстовые PDF.")
        return
    token = config.telegram_bot_token.get_secret_value()
    if not token:
        fail("Для загрузки файла не настроен Telegram-бот.")
        return
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/getFile",
            json={"file_id": metadata.get("file_id")},
        )
        response.raise_for_status()
        value = response.json()
        path = value.get("result", {}).get("file_path", "")
        if not value.get("ok") or not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path:
            fail("Telegram не предоставил файл для загрузки.")
            return
        with tempfile.TemporaryFile() as handle:
            size = 0
            async with client.stream(
                "GET", f"https://api.telegram.org/file/bot{token}/{path}"
            ) as remote:
                remote.raise_for_status()
                async for block in remote.aiter_bytes():
                    size += len(block)
                    if size > MAX_BYTES:
                        fail("PDF превышает 10 МБ.")
                        return
                    handle.write(block)
            with sessions.begin() as session:
                guard_operation(session, job.operation_id, lease)
            handle.seek(0)
            uploaded = await client.post(
                config.backend_url + f"/internal/operations/{job.operation_id}/file",
                headers={
                    "Authorization": "Bearer " + config.service_token.get_secret_value(),
                    "X-Job-Id": str(lease[0]),
                    "X-Lease-Token": str(lease[1]),
                },
                files={"file": (name, handle, "application/pdf")},
            )
            if uploaded.status_code in {413, 422}:
                fail("PDF превышает 10 МБ или имеет некорректный формат.")
                return
            uploaded.raise_for_status()

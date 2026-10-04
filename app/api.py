import hmac
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db import session_factory
from app.models import Checkpoint, Heartbeat, History, Job, Operation, Outbox, Update, now
from app.providers import usage_totals
from app.queue import LeaseLost, acknowledge, claim, heartbeat, renew
from app.tools import registry

app = FastAPI(title="Персональный AI-помощник", docs_url=None, redoc_url=None)


def auth(authorization: str = Header(default="")):
    token = settings().service_token.get_secret_value()
    if not token or not hmac.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "Unauthorized")


def update_identity(payload):
    callback = payload.get("callback_query")
    message = callback.get("message", {}) if callback else payload.get("message", {})
    sender = callback.get("from", {}) if callback else message.get("from", {})
    return sender.get("id"), message.get("chat", {}).get("id"), message, callback


@app.get("/health")
def health():
    with session_factory()() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/internal/updates", dependencies=[Depends(auth)])
def ingest(payload: dict):
    update_id = payload.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        raise HTTPException(422, "update_id required")
    owner, chat, message, callback = update_identity(payload)
    if owner != settings().allowed_telegram_user_id or owner == 0:
        return {"accepted": True, "ignored": True}
    if not isinstance(chat, int):
        raise HTTPException(422, "chat id required")
    sessions = session_factory()
    with sessions.begin() as session:
        created = session.scalar(
            insert(Update)
            .values(id=update_id, owner_id=owner, payload=payload)
            .on_conflict_do_nothing()
            .returning(Update.id)
        )
        if created is None:
            return {"accepted": True, "duplicate": True}
        op = Operation(
            update_id=update_id, owner_id=owner, chat_id=chat, timezone=settings().timezone
        )
        if not callback and isinstance(message.get("date"), int):
            try:
                op.reference_at = datetime.fromtimestamp(message["date"], UTC)
            except (ValueError, OverflowError, OSError):
                raise HTTPException(422, "Invalid message date") from None
        session.add(op)
        session.flush()
        if callback:
            kind = "callback"
        elif message.get("document"):
            kind = "document"
        else:
            kind = "agent"
            session.add(
                History(
                    operation_id=op.id,
                    owner_id=owner,
                    message={"role": "user", "content": message.get("text", "")},
                )
            )
        session.add(Job(key=f"update:{update_id}", operation_id=op.id, kind=kind, payload=payload))
        return {"accepted": True, "operation_id": str(op.id)}


class Offset(BaseModel):
    value: int = Field(ge=0)


@app.get("/internal/checkpoint", dependencies=[Depends(auth)])
def checkpoint():
    with session_factory()() as session:
        row = session.get(Checkpoint, "telegram")
        return {"value": row.value if row else 0}


@app.post("/internal/checkpoint", dependencies=[Depends(auth)])
def advance(offset: Offset):
    with session_factory().begin() as session:
        session.execute(
            insert(Checkpoint)
            .values(name="telegram", value=offset.value)
            .on_conflict_do_update(
                index_elements=[Checkpoint.name],
                set_={"value": text("greatest(checkpoints.value, excluded.value)")},
            )
        )
    return {"ok": True}


@app.post("/internal/outbox/claim", dependencies=[Depends(auth)])
def claim_outbox():
    with session_factory().begin() as session:
        row = claim(session, Outbox, settings().allowed_telegram_user_id)
        if not row:
            return None
        return {
            "id": str(row.id),
            "lease_token": str(row.lease_token),
            "kind": row.kind,
            "payload": row.payload,
        }


class Receipt(BaseModel):
    lease_token: uuid.UUID
    error: str | None = Field(default=None, pattern="^[a-zA-Z0-9_]{1,64}$")


@app.post("/internal/outbox/{item_id}/ack", dependencies=[Depends(auth)])
def ack(item_id: uuid.UUID, receipt: Receipt):
    try:
        with session_factory().begin() as session:
            acknowledge(session, Outbox, item_id, receipt.lease_token, receipt.error)
    except LeaseLost as exc:
        raise HTTPException(409, "Lease lost") from exc
    return {"ok": True}


@app.post("/internal/outbox/{item_id}/renew", dependencies=[Depends(auth)])
def renew_outbox(item_id: uuid.UUID, receipt: Receipt):
    try:
        with session_factory().begin() as session:
            renew(session, Outbox, item_id, receipt.lease_token)
    except LeaseLost as exc:
        raise HTTPException(409, "Lease lost") from exc
    return {"ok": True}


@app.post("/internal/heartbeat/{name}", dependencies=[Depends(auth)])
def beat(name: str):
    if name not in {"telegram", "background"}:
        raise HTTPException(422, "Unknown worker")
    with session_factory().begin() as session:
        heartbeat(session, name)
    return {"ok": True}


@app.get("/internal/health/{name}", dependencies=[Depends(auth)])
def worker_health(name: str):
    with session_factory()() as session:
        row = session.get(Heartbeat, name)
        if not row or row.seen_at < now() - timedelta(seconds=60):
            raise HTTPException(503, "Worker heartbeat expired")
    return {"status": "ok"}


@app.get("/internal/operations/{operation_id}/usage", dependencies=[Depends(auth)])
def operation_usage(operation_id: uuid.UUID):
    with session_factory()() as session:
        op = session.get(Operation, operation_id)
        if not op or op.owner_id != settings().allowed_telegram_user_id:
            raise HTTPException(404, "Operation not found")
        return usage_totals(session, operation_id)


@app.post("/internal/operations/{operation_id}/file", dependencies=[Depends(auth)])
async def upload(operation_id: uuid.UUID, file: UploadFile = File()):
    with session_factory()() as session:
        op = session.get(Operation, operation_id)
        if not op or op.owner_id != settings().allowed_telegram_user_id:
            raise HTTPException(404, "Operation not found")
    if registry.upload is None:
        raise HTTPException(503, "PDF processing is not installed")
    # Domain hook owns limits, media validation and atomic publication of the UUID-named file.
    return await registry.upload(
        op, Path(file.filename or "document.pdf").name, file.content_type, file
    )

import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db import session_factory
from app.models import Activity, Checkpoint, Heartbeat, History, Job, Operation, Outbox, Update, now
from app.providers import usage_totals
from app.queue import LeaseLost, acknowledge, claim, heartbeat, renew
from app.tools import registry


@asynccontextmanager
async def lifespan(app):
    from app.bootstrap import install
    from app.providers import YandexProvider

    config, sessions = settings(), session_factory()
    install(sessions, YandexProvider(config, sessions), config)
    yield


app = FastAPI(title="Персональный AI-помощник", docs_url=None, redoc_url=None, lifespan=lifespan)


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
def ingest(payload: dict, x_local_upload: bool = Header(default=False)):
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
        from app.privacy import owner_lock

        state = owner_lock(session, owner)
        durable_payload = payload
        if callback:
            durable_payload = {
                "update_id": update_id,
                "callback_query": {
                    "id": callback.get("id"),
                    "data": callback.get("data"),
                    "from": {"id": owner},
                    "message": {"chat": {"id": chat}},
                },
            }
        created = session.scalar(
            insert(Update)
            .values(id=update_id, owner_id=owner, payload=durable_payload)
            .on_conflict_do_nothing()
            .returning(Update.id)
        )
        if created is None:
            return {"accepted": True, "duplicate": True}
        op = Operation(
            update_id=update_id,
            owner_id=owner,
            chat_id=chat,
            timezone=settings().timezone,
            context_epoch=state.context_epoch,
        )
        if not callback and isinstance(message.get("date"), int):
            try:
                op.reference_at = datetime.fromtimestamp(message["date"], UTC)
                op.source_message_at = op.reference_at
            except (ValueError, OverflowError, OSError):
                raise HTTPException(422, "Invalid message date") from None
        session.add(op)
        session.flush()
        if callback:
            kind = "callback"
        elif message.get("document"):
            kind = "document"
        else:
            from app.data_controls import deletion_scope
            from app.memory_overview import overview_requested

            source = message.get("text", "")
            scope = deletion_scope(source)
            kind = (
                "data_prepare"
                if scope
                else ("memory_overview" if overview_requested(source) else "agent")
            )
            if scope:
                op.scenario = "data_control"
            session.add(
                History(
                    operation_id=op.id,
                    owner_id=owner,
                    message={"role": "user", "content": message.get("text", "")},
                )
            )
        op.deadline_at = op.received_at + timedelta(seconds=900 if kind == "document" else 90)
        if not callback:
            session.add(
                Activity(
                    operation_id=op.id,
                    document=kind == "document",
                    progress_at=now() + timedelta(seconds=0 if kind == "document" else 10),
                )
            )
        if x_local_upload and kind != "document":
            raise HTTPException(422, "Local upload requires document metadata")
        # Trusted local transfer uses the same durable ingress but publishes the file
        # through /file before scheduling indexing; it must never race a getFile job.
        if not x_local_upload:
            session.add(
                Job(
                    key=f"update:{update_id}",
                    operation_id=op.id,
                    kind=kind,
                    payload=durable_payload,
                )
            )
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
            "remaining_seconds": max(0, (row.delivery_deadline_at - now()).total_seconds()),
        }


class Receipt(BaseModel):
    lease_token: uuid.UUID
    error: str | None = Field(default=None, pattern="^[a-zA-Z0-9_]{1,64}$")
    send_latency_ms: int | None = Field(default=None, ge=0, le=300000)
    failure_class: str | None = Field(
        default=None, pattern="^(permanent|network|rate_limit|server|payload)$"
    )
    retry_after: float | None = Field(default=None, ge=0, le=86400)


@app.post("/internal/outbox/{item_id}/ack", dependencies=[Depends(auth)])
def ack(item_id: uuid.UUID, receipt: Receipt):
    try:
        with session_factory().begin() as session:
            acknowledge(
                session,
                Outbox,
                item_id,
                receipt.lease_token,
                receipt.error,
                receipt.failure_class,
                receipt.retry_after,
            )
            session.get(Outbox, item_id).send_latency_ms = receipt.send_latency_ms
    except LeaseLost as exc:
        raise HTTPException(409, "Lease lost") from exc
    return {"ok": True}


@app.post("/internal/outbox/{item_id}/renew", dependencies=[Depends(auth)])
def renew_outbox(item_id: uuid.UUID, receipt: Receipt):
    try:
        with session_factory().begin() as session:
            valid = renew(session, Outbox, item_id, receipt.lease_token)
        if not valid:
            raise HTTPException(409, "Lease lost")
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
async def upload(
    operation_id: uuid.UUID,
    file: UploadFile = File(),
    x_job_id: uuid.UUID | None = Header(default=None),
    x_lease_token: uuid.UUID | None = Header(default=None),
):
    with session_factory()() as session:
        op = session.get(Operation, operation_id)
        if not op or op.owner_id != settings().allowed_telegram_user_id:
            raise HTTPException(404, "Operation not found")
    if registry.upload is None:
        raise HTTPException(503, "PDF processing is not installed")
    if bool(x_job_id) != bool(x_lease_token):
        raise HTTPException(422, "Both lease headers are required")
    lease = (x_job_id, x_lease_token) if x_job_id else None
    if lease:
        with session_factory()() as session:
            job = session.get(Job, x_job_id)
            if not job or job.operation_id != operation_id:
                raise HTTPException(404, "Job not found")
    # Domain hook owns limits, media validation and atomic publication of the UUID-named file.
    try:
        return await registry.upload(
            op, Path(file.filename or "document.pdf").name, file.content_type, file, lease
        )
    except LeaseLost as exc:
        raise HTTPException(409, "Lease lost") from exc


@app.get("/internal/operations/{operation_id}/latency", dependencies=[Depends(auth)])
def latency(operation_id: uuid.UUID):
    from app.latency import operation_latency

    with session_factory()() as session:
        op = session.get(Operation, operation_id)
        if not op or op.owner_id != settings().allowed_telegram_user_id:
            raise HTTPException(404, "Operation not found")
        return operation_latency(session, op)


@app.post("/internal/activity/claim", dependencies=[Depends(auth)])
def claim_activity():
    from app.privacy import owner_lock

    with session_factory().begin() as session:
        owner_lock(session, settings().allowed_telegram_user_id)
        timestamp = now()
        activities = session.scalars(
            select(Activity)
            .join(Operation)
            .where(
                Operation.owner_id == settings().allowed_telegram_user_id,
                Operation.status != "cancelled",
                Operation.delivery_state != "failed",
                (Operation.status.not_in({"done", "error"}))
                | select(Outbox.id)
                .where(
                    Outbox.operation_id == Operation.id,
                    Outbox.purpose == "final",
                    Outbox.kind == "sendMessage",
                    Outbox.terminal_revision == Operation.terminal_revision,
                    Outbox.status.in_({"pending", "running"}),
                )
                .exists(),
                Activity.next_typing_at <= timestamp,
            )
            .with_for_update()
        ).all()
        result = []
        for activity in activities:
            op = session.get(Operation, activity.operation_id)
            activity.next_typing_at = timestamp + timedelta(seconds=4)
            if not activity.progress_sent and activity.progress_at <= timestamp:
                from app.queue import enqueue_text

                enqueue_text(
                    session,
                    op,
                    "Документ получил, обрабатываю…"
                    if activity.document
                    else "Запрос получил, ещё работаю над ответом…",
                    key_prefix=f"{op.id}:progress",
                    purpose="progress",
                )
                activity.progress_sent = True
            result.append({"operation_id": str(op.id), "chat_id": op.chat_id})
        return result


@app.post("/internal/activity/{operation_id}/ack", dependencies=[Depends(auth)])
def ack_activity(operation_id: uuid.UUID):
    from app.privacy import owner_lock

    with session_factory().begin() as session:
        op = session.get(Operation, operation_id)
        if not op or op.owner_id != settings().allowed_telegram_user_id:
            raise HTTPException(404, "Operation not found")
        owner_lock(session, op.owner_id)
        if op.first_feedback_at is None:
            op.first_feedback_at = now()
    return {"ok": True}

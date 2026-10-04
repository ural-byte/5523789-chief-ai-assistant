import uuid
from datetime import timedelta

from sqlalchemy import case, select

from app.models import Heartbeat, Job, Operation, Outbox, now

LEASE_SECONDS = 120


class LeaseLost(Exception):
    pass


def require_lease(session, item_type, item_id, token):
    from app.privacy import owner_lock

    original = session.get(item_type, item_id)
    if original and original.operation_id:
        op = session.get(Operation, original.operation_id)
        owner_lock(session, op.owner_id)
    row = session.get(item_type, item_id, with_for_update=True, populate_existing=True)
    if not row or row.status != "running" or row.lease_token != token or row.lease_until <= now():
        raise LeaseLost("lease expired")
    return row


def claim(session, item_type, owner_id=None):
    timestamp = now()
    if owner_id is not None:
        from app.privacy import owner_lock

        owner_lock(session, owner_id)
    if item_type is Job and owner_id is not None:
        active = session.scalar(
            select(Job.id)
            .join(Operation)
            .where(
                Operation.owner_id == owner_id, Job.status == "running", Job.lease_until > timestamp
            )
            .limit(1)
        )
        if active:
            return None
    query = select(item_type).where(
        item_type.available_at <= timestamp,
        (item_type.status == "pending")
        | ((item_type.status == "running") & (item_type.lease_until <= timestamp)),
    )
    if owner_id is not None:
        query = query.join(Operation).where(Operation.owner_id == owner_id)
    priority = (
        case((Job.kind == "callback", 0), (Job.kind == "data_cleanup", 1), else_=2)
        if item_type is Job
        else item_type.available_at
    )
    row = session.scalar(
        query.order_by(priority, item_type.available_at).with_for_update(skip_locked=True).limit(1)
    )
    if row:
        row.status = "running"
        row.lease_token = uuid.uuid4()
        row.lease_until = timestamp + timedelta(seconds=LEASE_SECONDS)
        row.attempts += 1
        if item_type is Job and row.operation_id:
            op = session.get(Operation, row.operation_id)
            if op.first_started_at is None:
                op.first_started_at = timestamp
    return row


def renew(session, item_type, item_id, token):
    row = require_lease(session, item_type, item_id, token)
    row.lease_until = now() + timedelta(seconds=LEASE_SECONDS)


def acknowledge(session, item_type, item_id, token, error=None):
    row = require_lease(session, item_type, item_id, token)
    row.error_code = error
    row.status = "done" if error is None else "pending"
    if error:
        row.available_at = now() + timedelta(seconds=min(300, 2 ** min(row.attempts, 8)))
    row.lease_until = None
    row.lease_token = None
    if item_type is Outbox and error is None:
        from app.latency import update_delivery

        row.acknowledged_at = now()
        op = session.get(Operation, row.operation_id)
        if op and row.kind == "sendMessage" and op.first_feedback_at is None:
            op.first_feedback_at = row.acknowledged_at
        if op:
            update_delivery(session, op)


def heartbeat(session, name):
    row = session.get(Heartbeat, name)
    if row:
        row.seen_at = now()
    else:
        session.add(Heartbeat(name=name))


def enqueue_text(session, operation, text_value, buttons=None, key_prefix=None, purpose="final"):
    # Telegram's limit is 4096 Unicode characters; leave room for server-side counting.
    chunks = []
    chunk = ""
    units = 0
    for char in text_value:
        size = len(char.encode("utf-16-le")) // 2
        if units + size > 4000:
            chunks.append(chunk)
            chunk, units = "", 0
        chunk += char
        units += size
    chunks.append(chunk or "Не удалось подготовить ответ. Попробуйте уточнить запрос.")
    for index, chunk in enumerate(chunks):
        key = f"{key_prefix or str(operation.id) + ':reply'}:{index}"
        if session.scalar(select(Outbox.id).where(Outbox.key == key)):
            continue
        payload = {"chat_id": operation.chat_id, "text": chunk}
        if buttons and index == len(chunks) - 1:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        session.add(
            Outbox(
                key=key,
                kind="sendMessage",
                payload=payload,
                operation_id=operation.id,
                purpose=purpose,
            )
        )

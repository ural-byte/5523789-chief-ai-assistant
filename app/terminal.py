"""Finite execution and delivery states, without fabricated Telegram receipts."""

import unicodedata

from sqlalchemy import select

from app.models import Activity, Job, Operation, Outbox, now

MAX_SEND_ATTEMPTS = 4
DELIVERY_SECONDS = 60
ERROR_TEXT = (
    "Не удалось завершить обработку или доставить ответ. "
    "Проверьте результат действия перед повторным запросом."
)


class InvalidFinal(Exception):
    pass


class ExecutionExpired(Exception):
    pass


def nonblank(value):
    return isinstance(value, str) and any(
        not char.isspace() and unicodedata.category(char)[0] not in {"C", "M", "Z"}
        for char in value
    )


def remaining(operation, timestamp=None):
    return (operation.deadline_at - (timestamp or now())).total_seconds()


def revoke(row):
    row.status, row.lease_token, row.lease_until = "cancelled", None, None


def stop_activity(session, operation):
    for row in session.scalars(
        select(Outbox).where(
            Outbox.operation_id == operation.id,
            Outbox.purpose == "progress",
            Outbox.status.in_({"pending", "running"}),
        )
    ):
        revoke(row)
    activity = session.get(Activity, operation.id)
    if activity:
        activity.progress_sent = True


def terminal_error(session, operation, reason, recovery=True):
    from app.queue import enqueue_text

    for row in session.scalars(
        select(Outbox).where(
            Outbox.operation_id == operation.id,
            Outbox.purpose == "final",
            Outbox.status.in_({"pending", "running"}),
        )
    ):
        revoke(row)
    for job in session.scalars(
        select(Job).where(
            Job.operation_id == operation.id,
            Job.kind != "data_cleanup",
            Job.status.in_({"pending", "running"}),
        )
    ):
        revoke(job)
    operation.status, operation.error_reason = "error", reason
    operation.processing_finished_at = operation.processing_finished_at or now()
    operation.final_delivery_ack_at = None
    stop_activity(session, operation)
    if recovery and not operation.recovery_created:
        operation.recovery_created = True
        operation.terminal_revision += 1
        operation.delivery_state = "pending"
        enqueue_text(
            session,
            operation,
            "Память изменилась. Откройте /memory заново, чтобы увидеть актуальные записи."
            if reason == "context_changed" and operation.scenario == "memory_overview"
            else ERROR_TEXT,
            key_prefix=f"{operation.id}:terminal-error",
            terminal_write=True,
        )
    else:
        operation.delivery_state = "failed"


def fail_delivery(session, row, failure_class):
    row.status, row.failure_class, row.terminal_failed_at = "failed", failure_class, now()
    row.lease_token = row.lease_until = None
    if row.purpose != "final" or row.kind != "sendMessage":
        return
    operation = session.get(Operation, row.operation_id)
    if operation and row.terminal_revision == operation.terminal_revision:
        terminal_error(session, operation, "delivery_failed")


def delivery_sweep(session, owner_id):
    from app.privacy import owner_lock

    owner_lock(session, owner_id)
    rows = session.scalars(
        select(Outbox)
        .join(Operation)
        .where(Operation.owner_id == owner_id, Outbox.status.in_({"pending", "running"}))
    ).all()
    for row in rows:
        if row.status not in {"pending", "running"}:
            continue
        if row.kind == "sendMessage" and not nonblank(row.payload.get("text")):
            fail_delivery(session, row, "payload")
        elif row.attempts >= MAX_SEND_ATTEMPTS and (
            row.status == "pending" or row.lease_until is None or row.lease_until <= now()
        ):
            fail_delivery(session, row, "attempts")
        elif row.delivery_deadline_at <= now():
            fail_delivery(session, row, "deadline")


def expire_operations(session, owner_id):
    from app.privacy import owner_lock

    owner_lock(session, owner_id)
    for operation in session.scalars(
        select(Operation)
        .where(
            Operation.owner_id == owner_id,
            Operation.status.not_in({"done", "error", "cancelled"}),
            Operation.deadline_at <= now(),
        )
        .with_for_update()
    ):
        terminal_error(session, operation, "deadline")

"""Content-free, nullable operation stage measurements."""

from sqlalchemy import select

from app.models import AICall, Outbox, now

TERMINAL = {"done", "error", "cancelled"}


def finish_operation(session, operation):
    if operation.status in TERMINAL and operation.processing_finished_at is None:
        operation.processing_finished_at = now()
        if operation.status == "cancelled":
            cancel_progress(session, operation)
        update_delivery(session, operation)


def update_delivery(session, operation):
    session.flush()
    rows = session.scalars(
        select(Outbox).where(
            Outbox.operation_id == operation.id,
            Outbox.purpose == "final",
            Outbox.terminal_revision == operation.terminal_revision,
            Outbox.kind == "sendMessage",
        )
    ).all()
    if (
        operation.status in TERMINAL
        and rows
        and all(r.status == "done" and r.acknowledged_at for r in rows)
    ):
        operation.delivery_state = "delivered"
        operation.final_delivery_ack_at = max(r.acknowledged_at for r in rows)
        cancel_progress(session, operation)


def cancel_progress(session, operation):
    for row in session.scalars(
        select(Outbox).where(
            Outbox.operation_id == operation.id,
            Outbox.purpose == "progress",
            Outbox.status == "pending",
        )
    ):
        row.status, row.payload = "cancelled", {}


def difference(a, b):
    return None if a is None or b is None or b < a else round((b - a).total_seconds() * 1000)


def operation_latency(session, operation):
    calls = session.scalars(select(AICall).where(AICall.operation_id == operation.id)).all()
    return {
        "operation_id": str(operation.id),
        "scenario": operation.scenario,
        "status": operation.status,
        "error_reason": operation.error_reason,
        "terminal_revision": operation.terminal_revision,
        "delivery_state": operation.delivery_state,
        "deadline_at": operation.deadline_at,
        "source_message_at": operation.source_message_at,
        "received_at": operation.received_at,
        "first_started_at": operation.first_started_at,
        "processing_finished_at": operation.processing_finished_at,
        "first_feedback_at": operation.first_feedback_at,
        "final_delivery_ack_at": operation.final_delivery_ack_at,
        "source_to_ingress_ms": difference(operation.source_message_at, operation.received_at),
        "queue_wait_ms": difference(operation.received_at, operation.first_started_at),
        "runtime_ms": difference(operation.first_started_at, operation.processing_finished_at),
        "retrieval_ms": operation.retrieval_ms,
        "llm_tool_loop_ms": operation.agent_loop_ms,
        "ai_ms": sum(c.latency_ms for c in calls)
        if calls and all(c.latency_ms is not None for c in calls)
        else (0 if not calls else None),
        "ai_calls": len(calls),
        "delivery_ms": difference(
            operation.processing_finished_at, operation.final_delivery_ack_at
        ),
        "end_to_end_ms": difference(operation.received_at, operation.final_delivery_ack_at),
        "source_to_ack_ms": difference(
            operation.source_message_at, operation.final_delivery_ack_at
        ),
    }

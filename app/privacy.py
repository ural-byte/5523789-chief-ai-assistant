"""Owner serialization and publication fences for data controls."""

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.models import History, Invocation, Job, Operation, Tombstone, Update, UserState
from app.queue import LeaseLost


class ContextChanged(Exception):
    pass


def owner_lock(session, owner_id):
    session.execute(text("SELECT pg_advisory_xact_lock(:owner)"), {"owner": owner_id})
    state = session.get(UserState, owner_id)
    if state is None:
        state = UserState(owner_id=owner_id, context_epoch=0)
        session.add(state)
        session.flush()
    return state


def guard_operation(session, operation_id, lease=None, context=False):
    from app.queue import require_lease

    op = session.get(Operation, operation_id)
    if op is None:
        raise LeaseLost("operation missing")
    state = owner_lock(session, op.owner_id)
    session.refresh(op)
    if session.get(Tombstone, f"operation:{op.id}") or op.status == "cancelled" or op.error_reason:
        raise LeaseLost("operation revoked")
    if lease:
        require_lease(session, Job, *lease)
    from app.terminal import ExecutionExpired

    timestamp = session.scalar(text("SELECT clock_timestamp()"))
    if op.deadline_at <= timestamp:
        raise ExecutionExpired()
    session.info.setdefault("execution_guards", {})[op.id] = (op.deadline_at, op.terminal_revision)
    if context and op.context_epoch != state.context_epoch:
        raise ContextChanged()
    return op


def rebuild_context(session, operation_id, lease=None, protocol="native"):
    op = guard_operation(session, operation_id, lease)
    state = session.get(UserState, op.owner_id)
    import json

    from app.runtime import result_message

    invocations = session.scalars(
        select(Invocation).where(Invocation.operation_id == op.id).order_by(Invocation.ordinal)
    ).all()
    retained = []
    for row in invocations:
        if session.get(Tombstone, f"invocation:{row.id}"):
            row.arguments, row.result, row.model_result = {}, {}, {}
            continue
        if (
            row.name in {"create_task", "prepare_meeting", "save_memory", "prepare_data_deletion"}
            and row.result
        ):
            retained.append(row)
        else:
            row.arguments, row.result, row.model_result = {}, {}, {}
    session.query(History).filter(History.operation_id == op.id).delete()
    update = session.get(Update, op.update_id) if op.update_id is not None else None
    source = update.payload.get("message", {}).get("text", "") if update else ""
    session.add(
        History(
            operation_id=op.id, owner_id=op.owner_id, message={"role": "user", "content": source}
        )
    )
    for inv in retained:
        session.add(
            History(
                operation_id=op.id,
                owner_id=op.owner_id,
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": inv.call_id,
                            "type": "function",
                            "function": {
                                "name": inv.name,
                                "arguments": json.dumps(inv.arguments, ensure_ascii=False),
                            },
                        }
                    ],
                }
                if protocol == "native"
                else {
                    "role": "assistant",
                    "content": json.dumps(
                        {"type": "tool", "name": inv.name, "arguments": inv.arguments},
                        ensure_ascii=False,
                    ),
                },
            )
        )
        session.add(
            History(
                operation_id=op.id,
                owner_id=op.owner_id,
                message=result_message(inv.call_id, inv.model_result or inv.result, protocol),
            )
        )
    op.context_epoch = state.context_epoch
    op.tool_steps = len(retained)


@event.listens_for(Session, "before_commit")
@event.listens_for(Session, "after_flush_postexec")
def remaining_commit_budget(session, flush_context=None):
    from app.terminal import ExecutionExpired

    for operation_id, (deadline, revision) in session.info.get("execution_guards", {}).items():
        timestamp = session.scalar(text("SELECT clock_timestamp()"))
        if deadline <= timestamp:
            raise ExecutionExpired()
        op = session.get(Operation, operation_id)
        if op.terminal_revision != revision or op.error_reason:
            raise LeaseLost("operation retired")
        milliseconds = max(1, min(5000, int((deadline - timestamp).total_seconds() * 1000)))
        session.execute(
            text("SELECT set_config('statement_timeout', :budget, true)"),
            {"budget": str(milliseconds)},
        )


def guard_invocation(session, ctx):
    op = guard_operation(session, ctx.operation_id, ctx.lease)
    if session.get(Tombstone, f"invocation:{ctx.idempotency_key}"):
        raise LeaseLost("invocation retired")
    if op.context_epoch != session.get(UserState, op.owner_id).context_epoch:
        raise ContextChanged()
    return op

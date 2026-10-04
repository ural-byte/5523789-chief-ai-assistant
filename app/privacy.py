"""Owner serialization and publication fences for data controls."""

from sqlalchemy import select, text

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
    if session.get(Tombstone, f"operation:{op.id}") or op.status == "cancelled":
        raise LeaseLost("operation revoked")
    if lease:
        require_lease(session, Job, *lease)
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

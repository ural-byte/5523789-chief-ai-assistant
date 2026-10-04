"""Prepared exact snapshots, owner-approved deletion and restartable cleanup."""

import hashlib
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select

from app.domain_actions import Approval, Task
from app.domain_memory import Chunk, Document, Entity, Fact, MemoryEntry, explicitly_requested
from app.models import (
    ApprovalAudit,
    DeletionCleanup,
    FileIntent,
    History,
    Invocation,
    Job,
    Operation,
    Outbox,
    Tombstone,
    Update,
    now,
)
from app.privacy import guard_operation, owner_lock
from app.queue import enqueue_text
from app.tools import ToolResult

COMMANDS = {"/clear_memory": "memory", "/delete_documents": "documents", "/reset": "reset"}
ALIASES = {
    "очисти память": "memory",
    "очисти долговременную память": "memory",
    "удали всю память": "memory",
    "удали мои документы": "documents",
    "удали все мои документы": "documents",
    "удали все документы": "documents",
    "сбрось мои данные": "reset",
    "удали все мои данные": "reset",
}


def normalize(source):
    return re.sub(r"\s+", " ", source.strip().casefold()).rstrip(".!?")


def deletion_scope(source):
    text = normalize(source)
    return COMMANDS.get(text) or ALIASES.get(text)


class DeletionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["memory", "documents", "reset"]


def ids(rows):
    return sorted(str(row.id) for row in rows)


def digest(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def prepare_deletion(session, operation, invocation_id, scope, file_directory=None):
    owner_lock(session, operation.owner_id)
    old = session.scalar(select(Approval).where(Approval.invocation_id == invocation_id))
    if old:
        return deletion_result(old)
    owner = operation.owner_id
    memory = (
        session.scalars(select(MemoryEntry).where(MemoryEntry.owner_id == owner)).all()
        if scope in {"memory", "reset"}
        else []
    )
    documents = (
        session.scalars(select(Document).where(Document.owner_id == owner)).all()
        if scope in {"documents", "reset"}
        else []
    )
    tasks = (
        session.scalars(select(Task).where(Task.owner_id == owner)).all()
        if scope == "reset"
        else []
    )
    meetings = (
        session.scalars(
            select(Approval).where(Approval.owner_id == owner, Approval.action_kind == "meeting")
        ).all()
        if scope == "reset"
        else []
    )
    operations = session.scalars(
        select(Operation).where(
            Operation.owner_id == owner,
            Operation.id != operation.id,
            Operation.scenario != "data_control",
        )
    ).all()
    captured = ids(operations)
    # Select producers for this scope; unrelated scheduled tasks and queued
    # requests survive partial deletion while their previous context is scrubbed.
    producer_ids = []
    for old_op in operations:
        update = session.get(Update, old_op.update_id) if old_op.update_id is not None else None
        source = update.payload.get("message", {}).get("text", "") if update else ""
        names = set(
            session.scalars(select(Invocation.name).where(Invocation.operation_id == old_op.id))
        )
        related = (
            scope == "reset"
            or (
                scope == "memory"
                and (
                    old_op.scenario in {"memory", "memory_overview"}
                    or explicitly_requested(source)
                    or names & {"save_memory", "search_memory"}
                    or re.search(
                        r"\b(?:запомн\w*|памят\w*|помнишь|помнит\w*|помни)\b", source, re.I
                    )
                )
            )
            or (
                scope == "documents"
                and (
                    old_op.scenario in {"pdf_index", "pdf_question"}
                    or names & {"search_document"}
                    or re.search(r"pdf|документ", source, re.I)
                    or bool(update and update.payload.get("message", {}).get("document"))
                )
            )
        )
        if related:
            producer_ids.append(str(old_op.id))
    producer_ids.sort()
    intents = (
        session.scalars(
            select(FileIntent).where(
                FileIntent.owner_id == owner,
                FileIntent.operation_id.in_([o.id for o in operations]),
            )
        ).all()
        if scope in {"documents", "reset"}
        else []
    )
    file_anomalies = sum(
        Path(d.file_path).name != f"{d.operation_id}.pdf"
        or (
            file_directory is not None
            and Path(d.file_path).parent.absolute() != Path(file_directory).absolute()
        )
        for d in documents
    )
    paths = sorted({i.path for i in intents} | {f"{d.operation_id}.pdf" for d in documents})
    payload = {
        "file_anomalies": file_anomalies,
        "scope": scope,
        "prepared_at": now().isoformat(),
        "owner_id": owner,
        "memory_ids": ids(memory),
        "document_ids": ids(documents),
        "task_ids": ids(tasks),
        "meeting_ids": ids(meetings),
        "operation_ids": captured,
        "producer_ids": producer_ids,
        "paths": paths,
        "control_ids": sorted(
            str(a.id)
            for a in session.scalars(
                select(Approval).where(
                    Approval.owner_id == owner,
                    Approval.action_kind == "data_deletion",
                    Approval.status == "pending",
                )
            )
        )
        if scope == "reset"
        else [],
        "counts": {
            "memory": len(memory),
            "documents": len(documents),
            "tasks": len(tasks),
            "meetings": len(meetings),
            "requests": len(captured),
        },
    }
    payload["hash"] = digest(payload)
    row = Approval(
        invocation_id=invocation_id,
        operation_id=operation.id,
        owner_id=owner,
        chat_id=operation.chat_id,
        payload=payload,
        action_kind="data_deletion",
        expires_at=now() + timedelta(hours=24),
    )
    session.add(row)
    session.flush()
    return deletion_result(row)


def deletion_result(row):
    p, c = row.payload, row.payload["counts"]
    label = {
        "memory": "Очистить долговременную память",
        "documents": "Удалить документы",
        "reset": "Сбросить мои данные",
    }[p["scope"]]
    text = (
        f"{label}?\nНа момент подготовки: записи памяти — {c['memory']}, "
        f"документы — {c['documents']}, поручения — {c['tasks']}, "
        f"встречи — {c['meetings']}.\n"
        f"Будут очищены предыдущие диалоги и копии данных из {c['requests']} запросов. "
        f"Связанных запросов, которые не смогут продолжить обработку: {len(p['producer_ids'])}.\n"
        "Это необратимо. Данные и запросы, созданные после подготовки, сохранятся. "
        "Контекст предыдущего разговора будет сброшен.\n"
    )
    if not any(c.values()):
        text += "Сейчас удалять нечего; сохранённые данные не изменятся.\n"
    if p["scope"] == "memory":
        text += "Документы, поручения и встречи сохранятся.\n"
    elif p["scope"] == "documents":
        text += "Память, поручения и встречи сохранятся.\n"
    text += (
        "Отправленные сообщения в Telegram и данные у AI-провайдера не удаляются. "
        "Технические показатели без содержимого сохранятся.\nПодтверждение действует 24 часа."
    )
    return ToolResult(
        status="ok",
        presentation="canonical",
        user_message=text,
        data={"approval_id": str(row.id), "payload": p},
        buttons=[
            [
                {"text": "Подтвердить", "callback_data": f"a:{row.id}:y"},
                {"text": "Отменить", "callback_data": f"a:{row.id}:n"},
            ]
        ],
    )


class PrepareDataDeletion:
    def __init__(self, file_directory=None):
        self.file_directory = file_directory

    arguments = DeletionArgs
    description = (
        "Подготовить удаление только по прямой просьбе пользователя; требует подтверждения."
    )

    async def prepare(self, ctx, args):
        return deletion_scope(ctx.source_update.get("message", {}).get("text", "")) == args.scope

    def apply(self, session, ctx, args, prepared):
        if not prepared:
            return ToolResult(
                status="needs_clarification",
                presentation="canonical",
                user_message="Для удаления явно попросите очистить память, "
                "удалить документы или сбросить свои данные.",
            )
        op = guard_operation(session, ctx.operation_id, ctx.lease)
        op.scenario = "data_control"
        return prepare_deletion(
            session, op, uuid.UUID(ctx.idempotency_key), args.scope, self.file_directory
        )


def fence_delete(session, row):
    p, owner = row.payload, row.owner_id
    if p.get("owner_id") != owner or p.get("hash") != digest(
        {k: v for k, v in p.items() if k != "hash"}
    ):
        raise ValueError("invalid_deletion_snapshot")
    if not set(p["producer_ids"]).issubset(p["operation_ids"]):
        raise ValueError("invalid_deletion_provenance")
    for model, key in (
        (Operation, "operation_ids"),
        (MemoryEntry, "memory_ids"),
        (Document, "document_ids"),
        (Task, "task_ids"),
        (Approval, "meeting_ids"),
        (Approval, "control_ids"),
    ):
        for item in session.scalars(
            select(model).where(model.id.in_([uuid.UUID(i) for i in p[key]]))
        ):
            if item.owner_id != owner:
                raise ValueError("foreign_snapshot_item")
    from app.file_store import ManagedFiles

    for path in p["paths"]:
        ManagedFiles.validate(path)
        if str(uuid.UUID(path[:36])) not in p["operation_ids"]:
            raise ValueError("foreign_snapshot_path")
    captured = [uuid.UUID(i) for i in p["operation_ids"]]
    scope = p["scope"]
    state = owner_lock(session, owner)
    state.context_epoch += 1
    revoked = [uuid.UUID(i) for i in p["producer_ids"]]
    for op_id in revoked:
        key = f"operation:{op_id}"
        if not session.get(Tombstone, key):
            session.add(Tombstone(key=key, owner_id=owner, approval_id=row.id))
    for kind in ("memory", "document", "task", "meeting"):
        for raw in p.get(f"{kind}_ids", []):
            key = f"{kind}:{raw}"
            if not session.get(Tombstone, key):
                session.add(Tombstone(key=key, owner_id=owner, approval_id=row.id))
    # Jobs and their leases are revoked under the same owner fence as publication.
    for job in session.scalars(select(Job).where(Job.operation_id.in_(captured))):
        if job.operation_id in revoked or job.status == "done":
            job.payload = {}
        if job.operation_id in revoked and job.kind != "callback" and job.status != "done":
            job.status, job.lease_token, job.lease_until = "cancelled", None, None
    for op in session.scalars(select(Operation).where(Operation.id.in_(captured))):
        if op.id in revoked and op.status not in {"done", "error"}:
            op.status = "cancelled"
        from app.latency import finish_operation

        finish_operation(session, op)
        if op.update_id is not None and (op.id in revoked or op.status in {"done", "error"}):
            update = session.get(Update, op.update_id)
            update.payload = {}
    for out in session.scalars(select(Outbox).where(Outbox.operation_id.in_(captured))):
        # Partial deletion retains already scheduled task reminders, independently
        # of scrubbing the originating dialog. Reset removes them with the tasks.
        if scope != "reset" and out.key.startswith("task:"):
            continue
        if out.status == "done" or out.operation_id in revoked:
            if out.status != "done":
                out.status, out.lease_token, out.lease_until = "cancelled", None, None
            out.payload = {}
    session.execute(delete(History).where(History.operation_id.in_(captured)))
    for inv in session.scalars(select(Invocation).where(Invocation.operation_id.in_(captured))):
        inv.arguments, inv.result, inv.model_result = {}, {}, {}
    if scope in {"memory", "reset"}:
        # Include every late result of the captured producers, not only rows
        # visible in the prepared card. New request provenance is preserved.
        invocation_ids = select(Invocation.id).where(Invocation.operation_id.in_(captured))
        entries = select(MemoryEntry.id).where(
            MemoryEntry.owner_id == owner,
            (MemoryEntry.id.in_([uuid.UUID(i) for i in p["memory_ids"]]))
            | MemoryEntry.invocation_id.in_(invocation_ids),
        )
        session.execute(delete(Fact).where(Fact.entry_id.in_(entries)))
        session.execute(delete(MemoryEntry).where(MemoryEntry.id.in_(entries)))
        session.execute(
            delete(Entity).where(Entity.owner_id == owner, ~Entity.id.in_(select(Fact.entity_id)))
        )
    paths = set(p["paths"])
    if scope in {"documents", "reset"}:
        docs = session.scalars(
            select(Document).where(
                Document.owner_id == owner,
                (Document.id.in_([uuid.UUID(i) for i in p["document_ids"]]))
                | Document.operation_id.in_(captured),
            )
        ).all()
        paths.update(f"{d.operation_id}.pdf" for d in docs)
        paths.update(
            session.scalars(
                select(FileIntent.path).where(
                    FileIntent.owner_id == owner, FileIntent.operation_id.in_(captured)
                )
            )
        )
        session.execute(delete(Chunk).where(Chunk.document_id.in_([d.id for d in docs])))
        session.execute(delete(Document).where(Document.id.in_([d.id for d in docs])))
    if scope == "reset":
        for previous in session.scalars(
            select(Approval).where(
                Approval.id.in_([uuid.UUID(i) for i in p["control_ids"]]),
                Approval.owner_id == owner,
                Approval.status == "pending",
            )
        ):
            previous.status = "cancelled"
            session.add(
                ApprovalAudit(
                    callback_id=f"reset:{row.id}:{previous.id}",
                    approval_id=previous.id,
                    operation_id=row.operation_id,
                    outcome="reset_cancelled",
                )
            )
        session.execute(
            delete(Task).where(
                Task.owner_id == owner,
                (Task.id.in_([uuid.UUID(i) for i in p["task_ids"]]))
                | Task.operation_id.in_(captured),
            )
        )
        for meeting in session.scalars(
            select(Approval).where(
                Approval.owner_id == owner,
                Approval.action_kind == "meeting",
                Approval.operation_id.in_(captured),
            )
        ):
            session.add(
                ApprovalAudit(
                    callback_id=f"reset:{row.id}:{meeting.id}",
                    approval_id=meeting.id,
                    operation_id=row.operation_id,
                    outcome="reset_removed",
                )
            )
            session.delete(meeting)
    row.status, row.approved_at = "executing", now()
    session.add(DeletionCleanup(approval_id=row.id, paths=sorted(paths), remaining=sorted(paths)))
    session.add(
        Job(
            key=f"deletion:{row.id}:cleanup",
            operation_id=row.operation_id,
            kind="data_cleanup",
            payload={"approval_id": str(row.id)},
        )
    )


async def cleanup_deletion(sessions, config, job, lease):
    from app.file_store import ManagedFiles
    from app.queue import require_lease

    approval_id = uuid.UUID(job.payload["approval_id"])
    try:
        with sessions.begin() as session:
            op = session.get(Operation, job.operation_id)
            owner_id = op.owner_id
            owner_lock(session, owner_id)
            require_lease(session, Job, *lease)
            cleanup = session.get(DeletionCleanup, approval_id)
            if session.get(Approval, approval_id).payload.get("file_anomalies"):
                raise ValueError("legacy_managed_path_anomaly")
            paths = list(cleanup.remaining)
        with ManagedFiles(config.file_directory) as files:
            for path in paths:
                files.unlink(path)
                if files.exists(path):
                    raise OSError("managed_path_remains")
                with sessions.begin() as session:
                    owner_lock(session, owner_id)
                    require_lease(session, Job, *lease)
                    cleanup = session.get(DeletionCleanup, approval_id, with_for_update=True)
                    cleanup.remaining = [p for p in cleanup.remaining if p != path]
                    cleanup.error_code = None
            # Reverify every name, including progress committed before a crash.
            for path in session_paths(sessions, approval_id):
                if files.exists(path):
                    raise OSError("managed_path_remains")
        with sessions.begin() as session:
            owner_lock(session, owner_id)
            require_lease(session, Job, *lease)
            row = session.get(Approval, approval_id, with_for_update=True)
            cleanup = session.get(DeletionCleanup, approval_id)
            if cleanup.remaining:
                raise OSError("cleanup_incomplete")
            if row.status == "executing":
                row.status, row.executed_at = "executed", now()
                session.add(
                    ApprovalAudit(
                        callback_id=f"completed:{row.id}",
                        approval_id=row.id,
                        operation_id=row.operation_id,
                        outcome="executed",
                    )
                )
                enqueue_text(
                    session,
                    session.get(Operation, row.operation_id),
                    (
                        "Удаление завершено. Указанные данные удалены необратимо; "
                        "новые данные сохранены."
                    )
                    if any(row.payload["counts"].values())
                    else "Удалять нечего. Сохранённые данные не изменены.",
                    key_prefix=f"deletion:{row.id}:complete",
                    purpose="notification",
                )
    except Exception as exc:
        from app.queue import LeaseLost

        if isinstance(exc, LeaseLost):
            raise
        with sessions.begin() as session:
            owner_lock(session, owner_id)
            require_lease(session, Job, *lease)
            session.get(DeletionCleanup, approval_id).error_code = type(exc).__name__
            enqueue_text(
                session,
                session.get(Operation, job.operation_id),
                "Удаление ещё не завершено. Продолжу автоматически; "
                "уже удалённые данные не восстановятся.",
                key_prefix=f"deletion:{approval_id}:retry",
                purpose="notification",
            )
        raise


def session_paths(sessions, approval_id):
    with sessions() as session:
        return list(session.get(DeletionCleanup, approval_id).paths)

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.bootstrap import install
from app.config import Settings
from app.data_controls import DeletionArgs, PrepareDataDeletion, deletion_scope
from app.domain_actions import Approval, Task, scheduler_once
from app.domain_memory import Document, Entity, Fact, MemoryEntry, SaveArgs, SaveMemory
from app.file_store import ManagedFiles, guarded_open, register_writer
from app.models import (
    ApprovalAudit,
    DeletionCleanup,
    History,
    Invocation,
    Job,
    Operation,
    Outbox,
    Tombstone,
    Update,
    now,
)
from app.privacy import guard_operation, rebuild_context
from app.queue import LeaseLost, claim
from app.workers import background_once
from tests.test_actions import ActionArgs, CreateTask, RelativeDate, callback, context
from tests.test_memory_documents import Embeddings, upload
from tests.test_storage import operation


async def memory(sessions, text="Иванов директор", owner=42):
    ctx = context(sessions, "Запомни: " + text, owner)
    args = SaveArgs(
        text=text, facts=[{"entity": "Иванов", "predicate": "роль", "value": text[:1000]}]
    )
    handler = SaveMemory(sessions, Embeddings())
    prepared = await handler.prepare(ctx, args)
    with sessions.begin() as session:
        op = session.get(Operation, ctx.operation_id)
        op.scenario = "memory"
        result = handler.apply(session, ctx, args, prepared)
    return uuid.UUID(result.data["entry_id"]), ctx


async def prepare(sessions, scope):
    source = {"memory": "/clear_memory", "documents": "/delete_documents", "reset": "/reset"}[scope]
    ctx = context(sessions, source)
    handler, args = PrepareDataDeletion(), DeletionArgs(scope=scope)
    ready = await handler.prepare(ctx, args)
    with sessions.begin() as session:
        result = handler.apply(session, ctx, args, ready)
    return uuid.UUID(result.data["approval_id"]), result


async def drain(sessions, tmp_path):
    config = Settings(allowed_telegram_user_id=42, file_directory=tmp_path)
    install(sessions, Embeddings(), config)
    while await background_once(sessions, Embeddings(), config):
        pass


@pytest.mark.parametrize(
    "source",
    [
        "Не очищай память",
        "Что означает /reset?",
        "«удали все мои данные»",
        "расскажи как удалить документы",
    ],
)
def test_source_gate_rejects_quoted_negated_questions(source):
    assert deletion_scope(source) is None


@pytest.mark.parametrize("scope", ["memory", "documents", "reset"])
async def test_prepare_only_cancel_foreign_expiry_and_immutable(sessions, tmp_path, scope):
    mid, _ = await memory(sessions)
    _, _, doc = await upload(sessions, tmp_path, ["private document"])
    aid, result = await prepare(sessions, scope)
    assert "необратимо" in result.user_message and "на момент" in result.user_message.casefold()
    with sessions() as session:
        original = session.get(Approval, aid).payload.copy()
        assert session.get(MemoryEntry, mid) and session.get(Document, doc)
    await callback(sessions, aid, owner=99)
    with sessions() as session:
        assert session.get(Approval, aid).status == "pending"
    await callback(sessions, aid, choice="n")
    await callback(sessions, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "cancelled"
        assert session.get(Approval, aid).payload == original
        assert session.get(MemoryEntry, mid)
        assert session.scalar(select(func.count()).select_from(ApprovalAudit)) == 3
    with pytest.raises(ValueError, match="immutable"), sessions.begin() as session:
        session.get(Approval, aid).payload = {"scope": "reset"}
    aid, _ = await prepare(sessions, scope)
    with sessions.begin() as session:
        session.get(Approval, aid).expires_at = now() - timedelta(seconds=1)
    await callback(sessions, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "expired"
        assert session.get(MemoryEntry, mid)


@pytest.mark.parametrize("scope", ["memory", "documents", "reset"])
async def test_exact_scope_new_data_other_owner_and_shared_entity_survive(
    sessions, tmp_path, scope
):
    old, oldctx = await memory(sessions, "старая роль")
    foreign, _ = await memory(sessions, "чужая роль", 99)
    _, _, olddoc = await upload(sessions, tmp_path, ["old document"])
    taskctx = context(sessions, "Напомни через два часа")
    with sessions.begin() as session:
        task = CreateTask().apply(
            session,
            taskctx,
            ActionArgs(
                text="task remains",
                date=RelativeDate(
                    kind="relative", source_phrase="через два часа", amount=2, unit="hours"
                ),
            ),
            (now() - timedelta(seconds=2), None),
        )
    aid, _ = await prepare(sessions, scope)
    new, _ = await memory(sessions, "новая роль")
    _, _, newdoc = await upload(sessions, tmp_path, ["new document"])
    await callback(sessions, aid)
    # Repeated yes/cancel cannot change executing/executed deletion nor its set.
    await callback(sessions, aid)
    await callback(sessions, aid, choice="n")
    await drain(sessions, tmp_path)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"
        assert session.get(MemoryEntry, foreign) and session.get(MemoryEntry, new)
        assert bool(session.get(MemoryEntry, old)) == (scope == "documents")
        assert bool(session.get(Document, olddoc)) == (scope == "memory")
        assert session.get(Document, newdoc)
        assert bool(session.get(Task, uuid.UUID(task.data["task_id"]))) == (scope != "reset")
        assert session.scalar(select(Fact).where(Fact.entry_id == new))
        assert session.scalar(select(Entity).where(Entity.owner_id == 42))
    scheduler_once(sessions, 42)
    if scope != "reset":
        with sessions() as session:
            assert session.scalar(
                select(Outbox).where(Outbox.key == f"task:{task.data['task_id']}:reminder")
            )


@pytest.mark.parametrize("opened", [False, True])
async def test_registered_writer_before_open_or_after_open_cannot_recreate(
    sessions, tmp_path, opened
):
    opid = operation(sessions)
    with sessions.begin() as session:
        session.get(Operation, opid).scenario = "pdf_index"
    part, final = register_writer(sessions, opid)
    with ManagedFiles(tmp_path) as files:
        handle = guarded_open(sessions, files, opid, part) if opened else None
        if handle:
            handle.write(b"private bytes")
            handle.flush()
        aid, _ = await prepare(sessions, "documents")
        await callback(sessions, aid)
        await drain(sessions, tmp_path)
        assert not files.exists(part) and not files.exists(final)
        with sessions() as session:
            assert session.get(Approval, aid).status == "executed"
        if handle:
            handle.write(b"still writes to unlinked inode")
            handle.close()
        with pytest.raises(LeaseLost):
            guarded_open(sessions, files, opid, part)
        with pytest.raises(LeaseLost), sessions.begin() as session:
            guard_operation(session, opid)
        assert not files.exists(part) and not files.exists(final)


async def test_cleanup_retry_crash_window_and_symlink_outside_untouched(
    sessions, tmp_path, monkeypatch
):
    opid = operation(sessions)
    with sessions.begin() as session:
        session.get(Operation, opid).scenario = "pdf_index"
    part, _ = register_writer(sessions, opid)
    target = tmp_path.parent / f"outside-{uuid.uuid4()}"
    target.write_text("outside remains")
    (tmp_path / part).symlink_to(target)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("unrelated remains")
    aid, _ = await prepare(sessions, "documents")
    await callback(sessions, aid)
    config = Settings(allowed_telegram_user_id=42, file_directory=tmp_path)
    install(sessions, Embeddings(), config)
    await background_once(sessions, Embeddings(), config)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executing"
        assert session.get(DeletionCleanup, aid).error_code == "ValueError"
        jobid = session.scalar(select(Job.id).where(Job.kind == "data_cleanup"))
    assert target.read_text() == "outside remains" and unrelated.exists()
    (tmp_path / part).unlink()
    with sessions.begin() as session:
        session.get(Job, jobid).available_at = now()
    await drain(sessions, tmp_path)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"
    assert target.exists() and unrelated.exists()
    target.unlink()
    link = tmp_path.parent / f"root-link-{uuid.uuid4()}"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError), ManagedFiles(link):
        pass
    link.unlink()


async def test_stale_external_memory_completion_fenced_and_new_context_rebuilt_without_duplicate(
    sessions,
):
    oldctx = context(sessions, "Запомни: старое")
    with sessions.begin() as session:
        session.get(Operation, oldctx.operation_id).scenario = "memory"
    handler = SaveMemory(sessions, Embeddings())
    prepared = await handler.prepare(oldctx, SaveArgs(text="старое"))
    aid, _ = await prepare(sessions, "memory")
    new, newctx = await memory(sessions, "новое")
    with sessions.begin() as session:
        inv = session.get(Invocation, uuid.UUID(newctx.idempotency_key))
        inv.name, inv.arguments = "save_memory", {"text": "новое", "facts": []}
        inv.result = {
            "status": "ok",
            "presentation": "canonical",
            "user_message": "saved",
            "data": {"entry_id": str(new)},
        }
    await callback(sessions, aid)
    with pytest.raises(LeaseLost), sessions.begin() as session:
        handler.apply(session, oldctx, SaveArgs(text="старое"), prepared)
    with sessions.begin() as session:
        rebuild_context(session, newctx.operation_id)
    another = context(sessions, "unused")
    from dataclasses import replace

    repeatctx = replace(newctx, idempotency_key=another.idempotency_key)
    with sessions.begin() as session:
        result = handler.apply(
            session,
            repeatctx,
            SaveArgs(text="новое"),
            await handler.prepare(repeatctx, SaveArgs(text="новое")),
        )
        assert result.data["entry_id"] == str(new)
    with sessions() as session:
        assert (
            len(session.scalars(select(MemoryEntry).where(MemoryEntry.owner_id == 42)).all()) == 1
        )
        assert session.scalar(
            select(History).where(
                History.operation_id == newctx.operation_id,
                History.message["role"].astext == "tool",
            )
        )


async def test_empty_reset_and_minimal_replay_audit_retained(sessions, tmp_path):
    opid = operation(sessions)
    with sessions.begin() as session:
        session.add(
            History(
                operation_id=opid,
                owner_id=42,
                message={"role": "user", "content": "secret raw content"},
            )
        )
    aid, result = await prepare(sessions, "reset")
    assert result.data["payload"]["counts"]["memory"] == 0
    await callback(sessions, aid)
    await drain(sessions, tmp_path)
    with sessions() as session:
        assert session.get(Operation, opid)
        assert not session.scalar(select(History).where(History.operation_id == opid))
        assert session.scalar(select(ApprovalAudit))
        assert session.get(Tombstone, f"operation:{opid}")


@pytest.mark.parametrize("second_choice", ["y", "n"])
async def test_concurrent_owner_clicks_have_single_execution(sessions, tmp_path, second_choice):
    from concurrent.futures import ThreadPoolExecutor

    from app.domain_actions import handle_callback

    mid, _ = await memory(sessions)
    aid, _ = await prepare(sessions, "memory")
    jobs = []
    for choice in ("y", second_choice):
        opid = operation(sessions)
        with sessions.begin() as session:
            job = Job(
                key=f"parallel:{opid}",
                operation_id=opid,
                kind="callback",
                payload={"callback_query": {"id": str(uuid.uuid4()), "data": f"a:{aid}:{choice}"}},
                status="running",
                lease_token=uuid.uuid4(),
                lease_until=now() + timedelta(minutes=2),
            )
            session.add(job)
            session.flush()
            jobs.append(job)

    def run(job):
        asyncio.run(handle_callback(sessions, job, (job.id, job.lease_token)))

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, j) for j in jobs]
        for f in futures:
            f.result(timeout=5)
    with sessions.begin() as session:
        for job in jobs:
            session.get(Job, job.id).status = "done"
        row = session.get(Approval, aid)
        assert row.status in {"executing", "cancelled"}
        expected = row.status
        assert len(session.scalars(select(DeletionCleanup)).all()) == int(expected == "executing")
    await drain(sessions, tmp_path)
    with sessions() as session:
        assert bool(session.get(MemoryEntry, mid)) == (expected == "cancelled")
        assert session.scalar(select(func.count()).select_from(ApprovalAudit)) == (
            3 if expected == "executing" else 2
        )


async def test_old_pending_unrelated_task_rebuilds_original_input(sessions, tmp_path):
    await memory(sessions)
    opid = operation(sessions)
    with sessions.begin() as session:
        update = Update(
            id=999, owner_id=42, payload={"message": {"text": "Напомни через два часа позвонить"}}
        )
        session.add(update)
        session.flush()
        session.get(Operation, opid).update_id = 999
        session.add(
            History(
                operation_id=opid,
                owner_id=42,
                message={"role": "user", "content": "Напомни через два часа позвонить"},
            )
        )
    aid, _ = await prepare(sessions, "memory")
    await callback(sessions, aid)
    with sessions.begin() as session:
        assert not session.get(Tombstone, f"operation:{opid}")
        rebuild_context(session, opid)
        assert (
            session.scalar(select(History).where(History.operation_id == opid))
            .message["content"]
            .startswith("Напомни")
        )
    ctx = context(sessions, "Напомни через два часа")
    from dataclasses import replace

    ctx = replace(ctx, operation_id=opid)
    with sessions.begin() as session:
        result = CreateTask().apply(
            session,
            ctx,
            ActionArgs(
                text="unrelated",
                date=RelativeDate(
                    kind="relative", source_phrase="через два часа", amount=2, unit="hours"
                ),
            ),
            (now() + timedelta(hours=2), None),
        )
        assert result.status == "ok"


async def test_crash_after_unlink_before_progress_and_before_unlink_retry(
    sessions, tmp_path, monkeypatch
):
    from app.data_controls import cleanup_deletion

    opid = operation(sessions)
    with sessions.begin() as session:
        session.get(Operation, opid).scenario = "pdf_index"
    part, final = register_writer(sessions, opid)
    with ManagedFiles(tmp_path) as files:
        with guarded_open(sessions, files, opid, part) as handle:
            handle.write(b"private")
    aid, _ = await prepare(sessions, "documents")
    await callback(sessions, aid)
    with sessions.begin() as session:
        job = claim(session, Job, 42)
    original = ManagedFiles.unlink
    crashed = False

    def crash_after_unlink(self, path):
        nonlocal crashed
        original(self, path)
        if not crashed:
            crashed = True
            raise OSError("simulated_crash_after_unlink")

    monkeypatch.setattr(ManagedFiles, "unlink", crash_after_unlink)
    config = Settings(allowed_telegram_user_id=42, file_directory=tmp_path)
    with pytest.raises(OSError):
        await cleanup_deletion(sessions, config, job, (job.id, job.lease_token))
    with sessions() as session:
        assert session.get(Approval, aid).status == "executing"
        assert session.get(DeletionCleanup, aid).remaining
    monkeypatch.setattr(ManagedFiles, "unlink", original)
    await cleanup_deletion(sessions, config, job, (job.id, job.lease_token))
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"
        assert not session.get(DeletionCleanup, aid).remaining
    assert not (tmp_path / part).exists() and not (tmp_path / final).exists()


async def test_legacy_metadata_outside_managed_root_cannot_claim_executed(sessions, tmp_path):
    _, _, docid = await upload(sessions, tmp_path, ["document"])
    with sessions.begin() as session:
        document = session.get(Document, docid)
        outside = tmp_path.parent / f"outside-{uuid.uuid4()}"
        outside.mkdir()
        target = outside / f"{document.operation_id}.pdf"
        target.write_bytes(b"outside bytes must remain")
        document.file_path = str(target)
    ctx = context(sessions, "/delete_documents")
    handler = PrepareDataDeletion(tmp_path)
    args = DeletionArgs(scope="documents")
    with sessions.begin() as session:
        result = handler.apply(session, ctx, args, True)
        aid = uuid.UUID(result.data["approval_id"])
    await callback(sessions, aid)
    await drain(sessions, tmp_path)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executing"
        assert session.get(DeletionCleanup, aid).error_code == "ValueError"
    assert target.read_bytes() == b"outside bytes must remain"
    target.unlink()
    outside.rmdir()


@pytest.mark.parametrize("backoff", [False, True])
@pytest.mark.parametrize(
    "source",
    [
        "Сохраните факт: Иванов директор",
        "Сохрани информацию: Иванов директор",
        "Сохрани этот факт: Иванов директор",
        "Сохраните этот факт: Иванов директор",
        "Пожалуйста, сохраните информацию: Иванов директор",
        "Прошу сохрани факт: Иванов директор",
        "Сохрани в память: Иванов директор",
        "Запомни: Иванов директор",
        "Запомните: Иванов директор",
        "Запомнить: Иванов директор",
    ],
)
async def test_every_memory_write_gate_alias_before_first_tool_is_revoked(
    sessions, tmp_path, source, backoff
):
    from app.domain_memory import explicitly_requested
    from app.tools import ToolContext

    assert explicitly_requested(source)
    old_id = operation(sessions)
    payload = {"update_id": 7001, "message": {"text": source}}
    with sessions.begin() as session:
        session.add(Update(id=7001, owner_id=42, payload=payload))
        session.flush()
        op = session.get(Operation, old_id)
        op.update_id = 7001
        op.scenario = "conversation"
        job = Job(
            key="old-alias-before-first-tool",
            operation_id=old_id,
            kind="agent",
            payload=payload,
            available_at=now() + timedelta(minutes=10),
            attempts=2 if backoff else 0,
            error_code="network" if backoff else None,
        )
        session.add(job)
        session.flush()
        jobid = job.id
        reference, timezone = op.reference_at, op.timezone
    aid, _ = await prepare(sessions, "memory")
    with sessions() as session:
        snapshot = session.get(Approval, aid).payload
        assert str(old_id) in snapshot["producer_ids"]
        assert str(old_id) in snapshot["operation_ids"]
    await callback(sessions, aid)
    await drain(sessions, tmp_path)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"
        assert session.get(Tombstone, f"operation:{old_id}")
        assert session.get(Update, 7001).payload == {}
        cancelled = session.get(Job, jobid)
        assert cancelled.status == "cancelled" and cancelled.payload == {}
        assert cancelled.lease_token is None
    with pytest.raises(LeaseLost), sessions.begin() as session:
        rebuild_context(session, old_id)
    # Model/embedding IO that had already started cannot bypass the publication
    # fence, even if its result arrives after cleanup has reported completion.
    ctx = ToolContext(42, 42, old_id, reference, timezone, str(uuid.uuid4()), payload)
    args = SaveArgs(text="Иванов директор")
    handler = SaveMemory(sessions, Embeddings())
    prepared = await handler.prepare(ctx, args)
    with pytest.raises(LeaseLost), sessions.begin() as session:
        handler.apply(session, ctx, args, prepared)
    with sessions() as session:
        assert not session.scalar(select(MemoryEntry).where(MemoryEntry.owner_id == 42))


async def test_repeated_cancel_data_deletion_uses_generic_action_text(sessions):
    aid, _ = await prepare(sessions, "memory")
    await callback(sessions, aid, choice="n")
    await callback(sessions, aid, choice="n")
    with sessions() as session:
        replies = [row.payload.get("text", "") for row in session.scalars(select(Outbox))]
    assert "Действие уже отменено." in replies
    assert not any("Встреча" in text for text in replies)

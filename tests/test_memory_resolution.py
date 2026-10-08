import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.domain_actions import Approval
from app.domain_memory import (
    Entity,
    Fact,
    MemoryEntry,
    SaveArgs,
    SaveMemory,
    SearchArgs,
    SearchMemory,
)
from app.memory_resolution import (
    ResolutionArgs,
    digest,
    prepare_resolution,
    remember_context,
)
from app.models import (
    ApprovalPreview,
    History,
    Invocation,
    MemoryContext,
    Operation,
    Outbox,
    Tombstone,
    Update,
    now,
)
from app.privacy import rebuild_context
from app.queue import LeaseLost, enqueue_text
from app.tools import ToolContext
from tests.test_actions import callback
from tests.test_memory_documents import Embeddings
from tests.test_storage import operation

SOURCE = "Актуальная версия — Петров. Старую запись удали."
OLD = "Иванов руководитель команды Альфа"
RETAIN = "Я руководитель команды Альфа, Петров"


def durable_context(sessions, source, name="save_memory", op_id=None):
    op_id = op_id or operation(sessions)
    with sessions.begin() as session:
        op = session.get(Operation, op_id)
        if op.update_id is None:
            update_id = int(uuid.uuid4().int % 900000000000000000)
            session.add(
                Update(
                    id=update_id,
                    owner_id=42,
                    payload={"update_id": update_id, "message": {"text": source}},
                )
            )
            session.flush()
            op.update_id = update_id
        inv = Invocation(operation_id=op_id, call_id=str(uuid.uuid4()), name=name, arguments={})
        session.add(inv)
        session.flush()
        return ToolContext(
            42, 42, op_id, op.reference_at, op.timezone, str(inv.id), {"message": {"text": source}}
        )


async def fixture_pair(
    sessions,
    shared=False,
    legacy=False,
    ack=True,
    old=OLD,
    retain=RETAIN,
    old_facts=None,
    retain_facts=None,
):
    provider = Embeddings()
    handler = SaveMemory(sessions, provider)
    contexts, ids = [], []
    for index, value in enumerate((old, retain)):
        ctx = durable_context(
            sessions,
            "Запомни: " + (old + "; " + retain if shared else value),
            op_id=contexts[0].operation_id if shared and contexts else None,
        )
        args = SaveArgs(
            text=value,
            facts=[
                {
                    "entity": "Иванов" if index == 0 else "я",
                    "predicate": "руководит" if index == 0 else "роль",
                    "value": value,
                }
            ],
        )
        if index == 0:
            args.facts.append(
                type(args.facts[0])(
                    entity="Иванов",
                    predicate="роль",
                    value="руководитель команды Альфа" if old == OLD else old,
                )
            )
        projections = old_facts if index == 0 else retain_facts
        if projections is not None:
            args = SaveArgs(text=value, facts=projections)
        ready = await handler.prepare(ctx, args)
        with sessions.begin() as session:
            result = handler.apply(session, ctx, args, ready)
            session.get(Invocation, uuid.UUID(ctx.idempotency_key)).result = result.model_dump()
            session.get(Invocation, uuid.UUID(ctx.idempotency_key)).arguments = args.model_dump()
        contexts.append(ctx)
        ids.append(uuid.UUID(result.data["entry_id"]))
    ctx = durable_context(sessions, "Кто руководит Альфой?", "search_memory")
    search = SearchMemory(sessions, provider)
    ready = await search.prepare(ctx, SearchArgs(query="Альфа"))
    with sessions.begin() as session:
        result = search.apply(session, ctx, SearchArgs(query="Альфа"), ready)
        # Actual production regression: all retrieved memory versions, only one fact, conflicts0.
        result.data["facts"] = result.data["facts"][-1:]
        result.data["conflicts"] = []
        session.get(Invocation, uuid.UUID(ctx.idempotency_key)).result = result.model_dump()
        op = session.get(Operation, ctx.operation_id)
        op.status = "done"
        enqueue_text(session, op, "Есть две версии руководства Альфой.")
        session.flush()
        row = session.scalar(select(Outbox).where(Outbox.operation_id == op.id))
        if ack:
            row.status, row.acknowledged_at = "done", now()
        if legacy:
            session.query(MemoryContext).filter(MemoryContext.operation_id == op.id).delete()
    return ids, contexts, ctx


def prepare(sessions, source=SOURCE, selector="Петров"):
    ctx = durable_context(sessions, source, "prepare_memory_resolution")
    with sessions.begin() as session:
        result = prepare_resolution(
            session,
            session.get(Operation, ctx.operation_id),
            uuid.UUID(ctx.idempotency_key),
            ResolutionArgs(selector=selector),
        )
        session.get(Invocation, uuid.UUID(ctx.idempotency_key)).result = result.model_dump()
        enqueue_text(
            session, session.get(Operation, ctx.operation_id), result.user_message, result.buttons
        )
    return (uuid.UUID(result.data["approval_id"]) if result.data else None), result


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("shared", [False, True])
async def test_exact_ack_pair_cross_self_two_old_facts_and_shared_producer(
    sessions, legacy, shared
):
    ids, contexts, retrieval = await fixture_pair(sessions, shared, legacy)
    aid, result = prepare(sessions)
    assert aid and OLD in result.user_message and RETAIN in result.user_message
    with sessions() as session:
        payload = session.get(Approval, aid).payload
        assert OLD not in str(payload) and RETAIN not in str(payload)
        assert len(payload["old"]["fact_ids"]) == 2
        assert session.get(MemoryEntry, ids[0]) and session.get(MemoryEntry, ids[1])
        retained_entry = session.get(MemoryEntry, ids[1])
        original = retained_entry.original
        embedding = list(retained_entry.embedding)
        retained_facts = payload["retain"]["fact_ids"]
    await callback(sessions, aid)
    await callback(sessions, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"
        assert not session.get(MemoryEntry, ids[0])
        assert session.get(MemoryEntry, ids[1])
        retained_entry = session.get(MemoryEntry, ids[1])
        assert retained_entry.original == original and list(retained_entry.embedding) == embedding
        assert sorted(
            str(f.id) for f in session.scalars(select(Fact).where(Fact.entry_id == ids[1]))
        ) == sorted(retained_facts)
        if shared:
            assert retained_entry.source_text == ""
        assert not session.get(ApprovalPreview, aid)
        assert session.get(Tombstone, f"invocation:{contexts[0].idempotency_key}")
        assert not session.get(Tombstone, f"operation:{contexts[1].operation_id}")
        for model, field in (
            (Invocation, "result"),
            (History, "message"),
            (Outbox, "payload"),
            (Update, "payload"),
        ):
            assert all(
                OLD not in str(getattr(row, field)) for row in session.scalars(select(model))
            )
    handler = SaveMemory(sessions, Embeddings())
    with pytest.raises(LeaseLost):
        await handler.prepare(contexts[0], SaveArgs(text=OLD))
    # Rebuild retains the live mutation replay without recreating old or duplicating retain.
    with sessions.begin() as session:
        rebuild_context(session, contexts[1].operation_id)
    assert await handler.prepare(contexts[1], SaveArgs(text=RETAIN)) == "existing"
    if shared:
        # Only a shared source was redacted; an independent retained source remains authorized.
        with sessions.begin() as session:
            inv = Invocation(
                operation_id=contexts[1].operation_id,
                call_id="late-new-call",
                name="save_memory",
                arguments={},
            )
            session.add(inv)
            session.flush()
            new_invocation = inv.id
        stale_ctx = ToolContext(
            42,
            42,
            contexts[1].operation_id,
            contexts[1].reference_at,
            contexts[1].timezone,
            str(new_invocation),
            contexts[1].source_update,
        )
        assert await handler.prepare(stale_ctx, SaveArgs(text=OLD)) is None
        ready = await Embeddings().embed(stale_ctx.operation_id, OLD)
        with sessions.begin() as session:
            result = handler.apply(session, stale_ctx, SaveArgs(text=OLD), ready)
            assert result.status == "needs_clarification"


@pytest.mark.parametrize(
    "source",
    [
        "Актуальная версия — Петров.",
        "Не удаляй старую запись",
        f"«{SOURCE}»",
        "Что значит удали старую запись?",
    ],
)
async def test_source_gate(sessions, source):
    await fixture_pair(sessions)
    aid, result = prepare(sessions, source)
    assert aid is None and result.status == "needs_clarification"


async def test_undelivered_context_does_not_grant_permission(sessions):
    await fixture_pair(sessions, ack=False)
    assert prepare(sessions)[0] is None


@pytest.mark.parametrize("choice", ["cancel", "expiry", "foreign", "foreign_chat"])
async def test_resolution_approval_no_delete_without_valid_owner_decision(sessions, choice):
    ids, _, _ = await fixture_pair(sessions)
    aid, _ = prepare(sessions)
    if choice == "expiry":
        with sessions.begin() as session:
            session.get(Approval, aid).expires_at = now() - timedelta(seconds=1)
    if choice == "foreign_chat":
        from app.domain_actions import handle_callback

        ctx = durable_context(sessions, "control", "callback")
        from app.models import Job
        from app.queue import claim

        with sessions.begin() as session:
            session.get(Operation, ctx.operation_id).chat_id = 99
            session.add(
                Job(
                    key=str(ctx.operation_id),
                    operation_id=ctx.operation_id,
                    kind="callback",
                    payload={"callback_query": {"id": str(uuid.uuid4()), "data": f"a:{aid}:y"}},
                )
            )
        with sessions.begin() as session:
            job = claim(session, Job, 42)
        await handle_callback(sessions, job, (job.id, job.lease_token))
    else:
        await callback(
            sessions,
            aid,
            owner=99 if choice == "foreign" else 42,
            choice="n" if choice == "cancel" else "y",
        )
    with sessions() as session:
        assert all(session.get(MemoryEntry, i) for i in ids)


async def test_other_preview_target_or_retain_stale_late_copy_and_new_data_preserved(sessions):
    ids, contexts, ctx = await fixture_pair(sessions)
    first, _ = prepare(sessions)
    second, _ = prepare(sessions)
    with sessions.begin() as session:
        original = session.get(Approval, second).payload
        # A different card treats OLD as retain; its payload contains only digests.
        third_ctx = durable_context(sessions, SOURCE, "prepare_memory_resolution")
        p = {
            **original,
            "old_entry_id": original["retain_entry_id"],
            "retain_entry_id": original["old_entry_id"],
            "old": original["retain"],
            "retain": original["old"],
        }
        p["hash"] = digest({k: v for k, v in p.items() if k != "hash"})
        third = Approval(
            invocation_id=uuid.UUID(third_ctx.idempotency_key),
            operation_id=third_ctx.operation_id,
            owner_id=42,
            chat_id=42,
            payload=p,
            action_kind="memory_resolution",
            expires_at=now() + timedelta(hours=24),
        )
        session.add(third)
        session.flush()
        third_id = third.id
        session.add(ApprovalPreview(approval_id=third_id, owner_id=42, text=OLD))
        enqueue_text(session, session.get(Operation, third.operation_id), OLD)
    # Independent newer entry with the same text is deliberately outside exact scope.
    newctx = durable_context(sessions, "Запомни: " + OLD)
    handler = SaveMemory(sessions, Embeddings())
    args = SaveArgs(text=OLD)
    ready = await handler.prepare(newctx, args)
    with sessions.begin() as session:
        new_id = uuid.UUID(handler.apply(session, newctx, args, ready).data["entry_id"])
    late = durable_context(sessions, "Кто руководит?", "search_memory")
    with sessions.begin() as session:
        remember_context(
            session,
            session.get(Operation, late.operation_id),
            uuid.UUID(late.idempotency_key),
            set(ids),
        )
        session.get(Invocation, uuid.UUID(late.idempotency_key)).result = {
            "data": {"memory": [{"id": str(ids[0]), "text": OLD}]}
        }
        enqueue_text(session, session.get(Operation, late.operation_id), OLD)
    await callback(sessions, first)
    for aid in (second, third_id):
        await callback(sessions, aid)
    with sessions() as session:
        assert session.get(MemoryEntry, new_id) and session.get(MemoryEntry, ids[1])
        for aid in (second, third_id):
            assert session.get(Approval, aid).status == "stale"
            assert not session.get(ApprovalPreview, aid)
        assert not session.scalar(
            select(MemoryContext).where(MemoryContext.operation_id == late.operation_id)
        )
        assert all(
            OLD not in str(out.payload)
            for out in session.scalars(
                select(Outbox).where(Outbox.operation_id == late.operation_id)
            )
        )


async def test_changed_complete_fact_composition_stales_confirmation(sessions):
    ids, _, _ = await fixture_pair(sessions)
    aid, _ = prepare(sessions)
    with sessions.begin() as session:
        entity = session.scalar(select(Entity))
        session.add(Fact(entry_id=ids[1], entity_id=entity.id, predicate="new", value="new"))
    await callback(sessions, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "stale"
        assert all(session.get(MemoryEntry, i) for i in ids)


async def test_unknown_action_kind_never_falls_through_to_meeting(sessions):
    ctx = durable_context(sessions, "control")
    with sessions.begin() as session:
        row = Approval(
            invocation_id=uuid.UUID(ctx.idempotency_key),
            operation_id=ctx.operation_id,
            owner_id=42,
            chat_id=42,
            action_kind="unrecognized",
            payload={"title": "unsafe"},
            expires_at=now() + timedelta(hours=24),
        )
        session.add(row)
        session.flush()
        aid = row.id
    await callback(sessions, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "pending"


@pytest.mark.parametrize(
    "text",
    [
        "Иванов руководитель команды Альфа и владелец компании Бета",
        "Иванов руководитель команды Альфа и консультант Гамма",
        "Иванов руководитель команды Альфа, мой телефон 12345",
        "Иванов руководитель команды Альфа; живёт в Москве",
    ],
)
async def test_mixed_independent_claims_are_not_silently_deleted(sessions, text):
    ids, _, _ = await fixture_pair(sessions)
    with sessions.begin() as session:
        session.get(MemoryEntry, ids[0]).original = text
        session.query(MemoryContext).delete()
    assert prepare(sessions)[0] is None


@pytest.mark.parametrize("change", ["original", "source", "extra_fact"])
async def test_legacy_backfill_rejects_changed_historical_boundaries(sessions, change):
    ids, _, _ = await fixture_pair(sessions, legacy=True)
    with sessions.begin() as session:
        entry = session.get(MemoryEntry, ids[0])
        if change == "original":
            entry.original += " changed"
        elif change == "source":
            entry.source_text += " changed"
        else:
            entity = session.scalar(select(Entity))
            session.add(
                Fact(entry_id=entry.id, entity_id=entity.id, predicate="extra", value="extra")
            )
    assert prepare(sessions)[0] is None


async def test_general_explicit_selector_and_unrelated_foreign_records_survive(sessions):
    ids, _, _ = await fixture_pair(sessions)
    aid, result = prepare(sessions, "Сохрани версию Петрова, удали старую запись.")
    assert aid and result.buttons
    from tests.test_data_controls import memory

    foreign, _ = await memory(sessions, "Директор компании Гамма Сидоров", owner=99)
    await callback(sessions, aid)
    with sessions() as session:
        assert session.get(MemoryEntry, foreign) and session.get(MemoryEntry, ids[1])


@pytest.mark.parametrize("scope", ["memory", "reset"])
async def test_clear_reset_scrubs_new_context_and_preview_rows(sessions, tmp_path, scope):
    await fixture_pair(sessions)
    selective, _ = prepare(sessions)
    from tests.test_data_controls import drain
    from tests.test_data_controls import prepare as prepare_all

    full, _ = await prepare_all(sessions, scope)
    await callback(sessions, full)
    await drain(sessions, tmp_path)
    with sessions() as session:
        assert not session.scalar(select(ApprovalPreview))
        assert not session.scalar(select(MemoryContext))
        assert session.get(Approval, selective).status in {"stale", "cancelled"}


@pytest.mark.parametrize("other_choice", ["y", "n"])
async def test_atomic_competing_owner_choices_never_execute_twice(sessions, other_choice):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from app.domain_actions import handle_callback
    from app.models import ApprovalAudit

    ids, _, _ = await fixture_pair(sessions)
    aid, _ = prepare(sessions)
    op_ids = [operation(sessions), operation(sessions)]
    barrier = threading.Barrier(2)

    def choose(index, choice):
        barrier.wait()
        job = SimpleNamespace(
            operation_id=op_ids[index],
            payload={"callback_query": {"id": str(uuid.uuid4()), "data": f"a:{aid}:{choice}"}},
        )
        asyncio.run(handle_callback(sessions, job, None))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(choose, 0, "y")
        second = pool.submit(choose, 1, other_choice)
        first.result(timeout=5)
        second.result(timeout=5)
    with sessions() as session:
        row = session.get(Approval, aid)
        assert row.status in {"executed", "cancelled"}
        assert bool(session.get(MemoryEntry, ids[0])) == (row.status == "cancelled")
        assert session.get(MemoryEntry, ids[1])
        assert (
            len(
                session.scalars(select(ApprovalAudit).where(ApprovalAudit.approval_id == aid)).all()
            )
            == 2
        )


@pytest.mark.parametrize(
    "old,retain",
    [
        ("Я директор отдела Альфа", "Директор отдела Бета — Петров"),
        ("Я директор компании Альфа", "Директор компании Бета — Петров"),
        ("Я руководитель проекта Альфа", "Руководитель проекта Бета — Петров"),
        ("Я руководитель направления Альфа", "Руководитель направления Бета — Петров"),
        ("Иванов директор отдела Альфа", "Петров директор отдела Бета, заместитель Иванов"),
        ("Я директор компании Альфа", "Директор отдела Альфа — Петров"),
        ("Я технический директор компании Альфа", "Финансовый директор компании Альфа — Петров"),
        ("Я директор по маркетингу компании Альфа", "Директор по продажам компании Альфа — Петров"),
        ("Я заместитель руководителя команды Альфа", "Руководитель команды Альфа — Петров"),
        ("Я директор отдела Альфа, руководитель проекта Бета", "Директор отдела Альфа — Петров"),
    ],
)
async def test_unrelated_named_units_never_create_destructive_approval(sessions, old, retain):
    ids, _, _ = await fixture_pair(sessions, old=old, retain=retain)
    aid, result = prepare(sessions)
    assert aid is None and result.status == "needs_clarification"
    with sessions() as session:
        assert not session.scalar(select(Approval))
        assert all(session.get(MemoryEntry, i) for i in ids)


@pytest.mark.parametrize(
    "old,retain",
    [
        ("Я директор отдела Альфа", "Отдел Альфа возглавляет Петров"),
        ("Команда Альфа: я руководитель", "Петров возглавляет команду Альфа"),
        ("Я руководитель команды «Альфа»", "Руководитель команды «Альфа» — Петров"),
        ("Я директор компании Альфа", "Компания Альфа: директор Петров"),
        ("Я руководитель проекта Альфа", "Проект Альфа возглавляет Петров"),
        ("Я руководитель направления Альфа", "Петров возглавляет направление Альфа"),
    ],
)
async def test_same_named_unit_role_paraphrases_keep_actual_cross_entity_flow(
    sessions, old, retain
):
    ids, _, _ = await fixture_pair(sessions, old=old, retain=retain)
    aid, result = prepare(sessions)
    assert aid and result.status == "ok"
    await callback(sessions, aid)
    with sessions() as session:
        assert not session.get(MemoryEntry, ids[0]) and session.get(MemoryEntry, ids[1])


async def test_structured_named_unit_provenance_supports_single_role_claim(sessions):
    ids, _, _ = await fixture_pair(sessions)
    with sessions.begin() as session:
        session.get(MemoryEntry, ids[0]).original = "Я директор"
        session.get(MemoryEntry, ids[1]).original = "Директор Петров"
        for entry_id in ids:
            entity = session.scalar(select(Entity).where(Entity.name == "иванов"))
            session.add(
                Fact(entry_id=entry_id, entity_id=entity.id, predicate="команда", value="Альфа")
            )
        session.query(MemoryContext).delete()
        inv = session.scalar(select(Invocation).where(Invocation.name == "search_memory"))
        remember_context(session, session.get(Operation, inv.operation_id), inv.id, set(ids))
    assert prepare(sessions)[0]


async def test_independent_unit_fact_projection_prevents_whole_entry_delete(sessions):
    ids, _, _ = await fixture_pair(sessions)
    with sessions.begin() as session:
        entity = session.scalar(select(Entity).where(Entity.name == "иванов"))
        session.add(Fact(entry_id=ids[0], entity_id=entity.id, predicate="проект", value="Бета"))
        session.query(MemoryContext).delete()
        inv = session.scalar(select(Invocation).where(Invocation.name == "search_memory"))
        remember_context(session, session.get(Operation, inv.operation_id), inv.id, set(ids))
    assert prepare(sessions)[0] is None
    with sessions() as session:
        assert not session.scalar(select(Approval))
        assert all(session.get(MemoryEntry, i) for i in ids)


@pytest.mark.parametrize("after_prepare", [False, True])
@pytest.mark.parametrize("delivery_status", ["pending", "running", "retry", "done"])
async def test_overview_provenance_retires_exact_copy_and_completes_delivery(
    sessions, after_prepare, delivery_status
):
    from app.latency import finish_operation
    from app.memory_overview import memory_page
    from app.queue import acknowledge

    ids, _, _ = await fixture_pair(sessions)
    aid = prepare(sessions)[0] if after_prepare else None
    overview_id = operation(sessions)
    receipt, lease = now(), uuid.uuid4()
    with sessions.begin() as session:
        op = session.get(Operation, overview_id)
        text, buttons = memory_page(session, op)
        op.scenario, op.status = "memory_overview", "done"
        enqueue_text(session, op, text, buttons)
        session.add(
            History(operation_id=op.id, owner_id=42, message={"role": "assistant", "content": text})
        )
        finish_operation(session, op)
        row = session.scalar(select(Outbox).where(Outbox.operation_id == op.id))
        assert OLD in row.payload["text"]
        row_id = row.id
        if delivery_status == "running":
            row.status, row.lease_token, row.lease_until = (
                "running",
                lease,
                now() + timedelta(seconds=120),
            )
        elif delivery_status == "done":
            row.status, row.acknowledged_at = "done", receipt
        elif delivery_status == "retry":
            row.attempts, row.error_code = 2, "network"
            row.available_at = now() + timedelta(seconds=4)
        metadata = session.scalar(select(MemoryContext).where(MemoryContext.operation_id == op.id))
        assert set(metadata.entries) == {str(i) for i in ids}
        assert OLD not in str(metadata.entries)
    aid = aid or prepare(sessions)[0]
    assert aid
    await callback(sessions, aid)
    with sessions() as session:
        op = session.get(Operation, overview_id)
        old_outbox = session.get(Outbox, row_id)
        assert old_outbox.payload == {}
        assert not session.scalar(select(History).where(History.operation_id == overview_id))
        assert not session.scalar(
            select(MemoryContext).where(MemoryContext.operation_id == overview_id)
        )
        assert session.get(MemoryEntry, ids[1]) and not session.get(MemoryEntry, ids[0])
        if delivery_status == "done":
            assert old_outbox.status == "done" and old_outbox.acknowledged_at == receipt
            assert op.final_delivery_ack_at == receipt and op.delivery_state == "delivered"
            assert op.terminal_revision == 0 and not op.recovery_created
        else:
            assert old_outbox.status == "cancelled" and old_outbox.lease_token is None
            assert op.status == "error" and op.error_reason == "context_changed"
            assert op.terminal_revision == 1 and op.recovery_created
            replacement = session.scalar(
                select(Outbox).where(Outbox.operation_id == overview_id, Outbox.status == "pending")
            )
            assert replacement and OLD not in replacement.payload["text"]
            assert "/memory" in replacement.payload["text"]
            replacement_id = replacement.id
    if delivery_status == "running":
        with pytest.raises(LeaseLost), sessions.begin() as session:
            acknowledge(session, Outbox, row_id, lease)
    if delivery_status != "done":
        with sessions.begin() as session:
            replacement = session.get(Outbox, replacement_id)
            replacement.status, replacement.lease_token = "running", lease
            replacement.lease_until = now() + timedelta(seconds=120)
            acknowledge(session, Outbox, replacement_id, lease)
        with sessions() as session:
            op = session.get(Operation, overview_id)
            assert op.delivery_state == "delivered" and op.final_delivery_ack_at
            assert (
                len(
                    session.scalars(
                        select(Outbox).where(
                            Outbox.operation_id == overview_id, Outbox.terminal_revision == 1
                        )
                    ).all()
                )
                == 1
            )


async def test_pagination_provenance_is_exact_and_preserves_unrelated_render(sessions, monkeypatch):
    from app import memory_overview

    ids, _, _ = await fixture_pair(sessions)
    monkeypatch.setattr(memory_overview, "PAGE_SIZE", 1)
    aid, _ = prepare(sessions)
    copies = []
    for cursor in (None, ids[0]):
        op_id = operation(sessions)
        with sessions.begin() as session:
            op = session.get(Operation, op_id)
            text, buttons = memory_overview.memory_page(session, op, cursor)
            op.scenario, op.status = "memory_overview", "done"
            enqueue_text(session, op, text, buttons)
            context = session.scalar(
                select(MemoryContext).where(MemoryContext.operation_id == op_id)
            )
            assert set(context.entries) == {str(ids[0] if cursor is None else ids[1])}
        copies.append(op_id)
    await callback(sessions, aid)
    with sessions() as session:
        erased = session.scalar(
            select(Outbox).where(Outbox.operation_id == copies[0], Outbox.terminal_revision == 0)
        )
        preserved = session.scalar(select(Outbox).where(Outbox.operation_id == copies[1]))
        assert erased.status == "cancelled" and erased.payload == {}
        assert preserved.status == "pending" and RETAIN in preserved.payload["text"]
        assert session.scalar(select(MemoryContext).where(MemoryContext.operation_id == copies[1]))


@pytest.mark.parametrize(
    "clause",
    [
        "у меня двое детей",
        "мне 37 лет",
        "моё хобби шахматы",
        "люблю кофе",
        "проживаю в Казани",
        "отвечаю за закупки",
        "мой телефон 12345",
    ],
)
async def test_full_original_independent_thought_never_creates_approval(sessions, clause):
    ids, _, _ = await fixture_pair(sessions, old=f"Я руководитель команды Альфа, {clause}")
    aid, result = prepare(sessions)
    assert aid is None and result.status == "needs_clarification"
    with sessions() as session:
        assert all(session.get(MemoryEntry, i) for i in ids)
        assert not session.scalar(select(Approval))


@pytest.mark.parametrize("entry_index", [0, 1])
@pytest.mark.parametrize(
    "predicate,value",
    [
        ("дети", "двое"),
        ("возраст", "37"),
        ("любимый напиток", "кофе"),
        ("роль", "отвечаю за закупки"),
    ],
)
async def test_complete_structured_facts_independent_of_pure_original_require_clarification(
    sessions, entry_index, predicate, value
):
    ids, contexts, retrieval = await fixture_pair(sessions)
    with sessions.begin() as session:
        entity = session.scalar(
            select(Entity).where(Entity.name == ("иванов" if entry_index == 0 else "я"))
        )
        session.add(
            Fact(entry_id=ids[entry_index], entity_id=entity.id, predicate=predicate, value=value)
        )
        producer = session.get(Invocation, uuid.UUID(contexts[entry_index].idempotency_key))
        producer.arguments = {
            **producer.arguments,
            "facts": [
                *producer.arguments["facts"],
                {"entity": entity.name, "predicate": predicate, "value": value},
            ],
        }
        # The complete extra fact is in the actual delivered boundary, not a stale scope race.
        session.query(MemoryContext).filter(
            MemoryContext.operation_id == retrieval.operation_id
        ).delete()
        remember_context(
            session,
            session.get(Operation, retrieval.operation_id),
            uuid.UUID(retrieval.idempotency_key),
            set(ids),
        )
    aid, result = prepare(sessions)
    assert aid is None and result.status == "needs_clarification"
    with sessions() as session:
        assert all(session.get(MemoryEntry, i) for i in ids)
        assert not session.scalar(select(Approval))


async def test_same_unpunctuated_extra_clause_cannot_be_treated_as_unit_name(sessions):
    await fixture_pair(
        sessions,
        old="Я руководитель команды Альфа у меня двое детей",
        retain="Петров руководитель команды Альфа у меня двое детей",
    )
    assert prepare(sessions)[0] is None


@pytest.mark.parametrize("status", ["pending", "running", "done"])
@pytest.mark.parametrize("truncated", [False, True])
async def test_legacy_canonical_overview_closure_preserves_receipts_and_unrelated_views(
    sessions, status, truncated
):
    from app.latency import finish_operation
    from app.memory_overview import render_entry

    ids, _, retrieval = await fixture_pair(sessions)
    if truncated:
        with sessions.begin() as session:
            entity = session.scalar(select(Entity).where(Entity.name == "иванов"))
            for _ in range(5):
                session.add(
                    Fact(
                        entry_id=ids[0],
                        entity_id=entity.id,
                        predicate="роль",
                        value="руководитель команды Альфа" + " " * 850,
                    )
                )
            session.query(MemoryContext).filter(
                MemoryContext.operation_id == retrieval.operation_id
            ).delete()
            remember_context(
                session,
                session.get(Operation, retrieval.operation_id),
                uuid.UUID(retrieval.idempotency_key),
                set(ids),
            )
    aid, _ = prepare(sessions)
    assert aid
    with sessions() as session:
        old = session.get(MemoryEntry, ids[0])
        facts = session.execute(
            select(Fact, Entity)
            .join(Entity, Fact.entity_id == Entity.id)
            .where(Fact.entry_id == ids[0])
        ).all()
        # Historical fact ordering is intentionally different from the current query.
        record = render_entry(old, list(reversed(facts)))
        body = "Сохранённые записи: 2. На этой странице: 1.\n\n" + record
        assert ("[Запись сокращена для показа]" in body) == truncated
    own = operation(sessions)
    foreign = operation(sessions, owner=99)
    unrelated = operation(sessions)
    another_chat = operation(sessions)
    lease, receipt = uuid.uuid4(), now()
    for op_id in (own, foreign, unrelated, another_chat):
        with sessions.begin() as session:
            op = session.get(Operation, op_id)
            op.scenario, op.status = "memory_overview", "done"
            if op_id == unrelated:
                op.scenario = "conversation"
            if op_id == another_chat:
                op.chat_id = 43
            enqueue_text(session, op, body)
            session.add(
                History(
                    operation_id=op.id,
                    owner_id=op.owner_id,
                    message={"role": "assistant", "content": body},
                )
            )
            finish_operation(session, op)
            row = session.scalar(select(Outbox).where(Outbox.operation_id == op_id))
            if op_id == own:
                own_row = row.id
                if status == "running":
                    row.status, row.lease_token, row.lease_until = (
                        status,
                        lease,
                        now() + timedelta(seconds=120),
                    )
                elif status == "done":
                    row.status, row.acknowledged_at = status, receipt
    await callback(sessions, aid)
    with sessions() as session:
        assert not session.get(MemoryEntry, ids[0]) and session.get(MemoryEntry, ids[1])
        out = session.get(Outbox, own_row)
        assert out.payload == {}
        assert not session.scalar(select(History).where(History.operation_id == own))
        op = session.get(Operation, own)
        if status == "done":
            assert out.status == "done" and op.final_delivery_ack_at == receipt
            assert op.delivery_state == "delivered" and not op.recovery_created
        else:
            assert out.status == "cancelled" and out.lease_token is None
            assert op.error_reason == "context_changed" and op.recovery_created
        for op_id in (foreign, unrelated, another_chat):
            untouched = session.scalar(select(Outbox).where(Outbox.operation_id == op_id))
            assert untouched.status == "pending" and untouched.payload["text"] == body
            assert session.scalar(select(History).where(History.operation_id == op_id))


async def test_new_tracked_identical_original_is_not_legacy_content_authority(sessions):
    from app.memory_overview import memory_page

    ids, _, _ = await fixture_pair(sessions)
    aid, _ = prepare(sessions)
    # Independently saved after prepare; same visible words do not identify the retired ID.
    ctx = durable_context(sessions, "Запомни: " + OLD)
    handler = SaveMemory(sessions, Embeddings())
    args = SaveArgs(text=OLD, facts=[{"entity": "Иванов", "predicate": "роль", "value": OLD}])
    ready = await handler.prepare(ctx, args)
    with sessions.begin() as session:
        result = handler.apply(session, ctx, args, ready)
        new_id = uuid.UUID(result.data["entry_id"])
    new_view_id = operation(sessions)
    with sessions.begin() as session:
        op = session.get(Operation, new_view_id)
        text, buttons = memory_page(session, op, ids[1])
        op.scenario, op.status = "memory_overview", "done"
        enqueue_text(session, op, text, buttons)
        context = session.scalar(
            select(MemoryContext).where(MemoryContext.operation_id == new_view_id)
        )
        assert set(context.entries) == {str(new_id)}
    await callback(sessions, aid)
    with sessions() as session:
        assert session.get(MemoryEntry, new_id) and session.get(MemoryEntry, ids[1])
        out = session.scalar(select(Outbox).where(Outbox.operation_id == new_view_id))
        assert out.status == "pending" and OLD in out.payload["text"]
        assert session.scalar(
            select(MemoryContext).where(MemoryContext.operation_id == new_view_id)
        )


async def test_revoked_overview_has_finite_typing_after_safe_replacement_ack(sessions, monkeypatch):
    from app import api
    from app.config import Settings
    from app.memory_overview import memory_page
    from app.models import Activity
    from app.queue import acknowledge

    _, _, _ = await fixture_pair(sessions)
    aid, _ = prepare(sessions)
    op_id = operation(sessions)
    with sessions.begin() as session:
        op = session.get(Operation, op_id)
        text, buttons = memory_page(session, op)
        op.scenario, op.status = "memory_overview", "done"
        enqueue_text(session, op, text, buttons)
        session.add(Activity(operation_id=op_id, progress_at=now() - timedelta(seconds=1)))
    await callback(sessions, aid)
    monkeypatch.setattr(api, "session_factory", lambda: sessions)
    monkeypatch.setattr(api, "settings", lambda: Settings(allowed_telegram_user_id=42))
    assert str(op_id) in {r["operation_id"] for r in api.claim_activity()}
    with sessions.begin() as session:
        row = session.scalar(
            select(Outbox).where(Outbox.operation_id == op_id, Outbox.terminal_revision == 1)
        )
        token = uuid.uuid4()
        row.status, row.lease_token, row.lease_until = (
            "running",
            token,
            now() + timedelta(seconds=120),
        )
        acknowledge(session, Outbox, row.id, token)
        session.get(Activity, op_id).next_typing_at = now() - timedelta(seconds=1)
    assert str(op_id) not in {r["operation_id"] for r in api.claim_activity()}


NARRATOR_FACTS = [
    {"entity": "Соколов", "predicate": "роль", "value": "руководитель команды Альфа"},
    {"entity": "Соколов", "predicate": "команда", "value": "Альфа"},
]
UNIT_PERSON_FACTS = [
    {"entity": "команда Альфа", "predicate": "руководитель", "value": "Петров"},
]
UNIT_PERSON_ORIGINAL = "Руководитель команды Альфа — Петров"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "original",
    [
        "Соколов сообщил: «я – руководитель команды Альфа».",
        'Соколов сказал: "я руководитель команды Альфа".',
    ],
)
async def test_known_attribution_full_quote_two_role_unit_facts_preserves_exact_pair(
    sessions, legacy, original
):
    ids, _, _ = await fixture_pair(
        sessions,
        legacy=legacy,
        old=original,
        retain=UNIT_PERSON_ORIGINAL,
        old_facts=NARRATOR_FACTS,
        retain_facts=UNIT_PERSON_FACTS,
    )
    aid, result = prepare(sessions)
    assert aid and result.status == "ok"
    assert original in result.user_message
    with sessions() as session:
        row = session.get(Approval, aid)
        assert len(row.payload["old"]["fact_ids"]) == 2
        assert row.payload["old_entry_id"] == str(ids[0])
    await callback(sessions, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"
        assert not session.get(MemoryEntry, ids[0]) and session.get(MemoryEntry, ids[1])


@pytest.mark.parametrize(
    "original",
    [
        "Неизвестный сообщил: «я руководитель команды Альфа».",
        "Соколов не сообщил: «я руководитель команды Альфа».",
        "Соколов возможно сообщил: «я руководитель команды Альфа».",
        "Соколов сообщает: «я руководитель команды Альфа».",
        "Вчера Соколов сообщил: «я руководитель команды Альфа».",
        "Соколов сообщил: «я руководитель команды Альфа», у меня двое детей.",
        "Соколов сообщил: «я руководитель команды Альфа, у меня двое детей».",
        "Соколов сообщил: «я руководитель команды Альфа». «Мне 37 лет».",
        "Соколов сообщил: я руководитель команды Альфа.",
        "Я сообщил руководитель команды Альфа",
    ],
)
async def test_attribution_does_not_consume_unknown_negated_or_extra_thoughts(sessions, original):
    ids, _, _ = await fixture_pair(
        sessions,
        old=original,
        retain=UNIT_PERSON_ORIGINAL,
        old_facts=NARRATOR_FACTS,
        retain_facts=UNIT_PERSON_FACTS,
    )
    aid, result = prepare(sessions)
    assert aid is None and result.status == "needs_clarification"
    with sessions() as session:
        assert not session.scalar(select(Approval))
        assert all(session.get(MemoryEntry, i) for i in ids)


async def test_attribution_still_checks_every_fact_not_only_inner_quote(sessions):
    await fixture_pair(
        sessions,
        old="Соколов сообщил: «я руководитель команды Альфа».",
        retain=UNIT_PERSON_ORIGINAL,
        old_facts=[*NARRATOR_FACTS, {"entity": "Соколов", "predicate": "дети", "value": "двое"}],
        retain_facts=UNIT_PERSON_FACTS,
    )
    assert prepare(sessions)[0] is None


async def test_typed_unit_person_projection_supports_reverse_choice_and_stales_other_card(sessions):
    ids, _, _ = await fixture_pair(
        sessions,
        old="Я руководитель команды Альфа",
        retain=UNIT_PERSON_ORIGINAL,
        old_facts=[
            {"entity": "я", "predicate": "роль", "value": "руководитель"},
            {"entity": "я", "predicate": "команда", "value": "Альфа"},
        ],
        retain_facts=UNIT_PERSON_FACTS,
    )
    forward, _ = prepare(sessions)
    ctx = durable_context(
        sessions, "Актуальная версия — Я. Старую запись удали.", "prepare_memory_resolution"
    )
    with sessions.begin() as session:
        result = prepare_resolution(
            session,
            session.get(Operation, ctx.operation_id),
            uuid.UUID(ctx.idempotency_key),
            ResolutionArgs(selector="Я"),
        )
        assert result.status == "ok"
        reverse = uuid.UUID(result.data["approval_id"])
        assert session.get(Approval, reverse).payload["retain_entry_id"] == str(ids[0])
    assert forward
    await callback(sessions, forward)
    with sessions() as session:
        assert session.get(Approval, reverse).status == "stale"
        assert not session.get(ApprovalPreview, reverse)
        assert session.get(MemoryEntry, ids[1]) and not session.get(MemoryEntry, ids[0])
    await callback(sessions, reverse)
    with sessions() as session:
        assert session.get(Approval, reverse).status == "stale"


@pytest.mark.parametrize(
    "value", ["Петров, у него двое детей", "Мне 37 лет", "Люблю кофе", "Петров отвечает за закупки"]
)
async def test_role_person_projection_never_consumes_an_independent_clause(sessions, value):
    await fixture_pair(
        sessions,
        old="Я руководитель команды Альфа",
        retain=UNIT_PERSON_ORIGINAL,
        retain_facts=[{"entity": "команда Альфа", "predicate": "руководитель", "value": value}],
    )
    assert prepare(sessions)[0] is None


@pytest.mark.parametrize(
    "clause",
    [
        "у меня двое детей",
        "мне 37 лет",
        "моё хобби шахматы",
        "люблю кофе",
        "проживаю в Казани",
        "отвечаю за закупки",
        "мой телефон 12345",
    ],
)
@pytest.mark.parametrize("embedded", [False, True])
async def test_sentence_entity_is_not_authority_for_original_or_fact(sessions, clause, embedded):
    original = "Я руководитель команды Альфа" + (f", {clause}" if embedded else "")
    ids, _, _ = await fixture_pair(
        sessions,
        old=original,
        old_facts=[{"entity": clause, "predicate": "роль", "value": "руководитель команды Альфа"}],
    )
    aid, result = prepare(sessions)
    assert aid is None and result.status == "needs_clarification"
    with sessions() as session:
        assert not session.scalar(select(Approval))
        assert all(session.get(MemoryEntry, entry_id) for entry_id in ids)


@pytest.mark.parametrize(
    "original,entity",
    [
        ("У меня двое детей сообщил: «я руководитель команды Альфа»", "у меня двое детей"),
        ("Люблю кофе сообщил: «я руководитель команды Альфа»", "люблю кофе"),
        (
            "Соколов сообщил: «я руководитель команды Альфа, люблю кофе»",
            "люблю кофе",
        ),
        (
            "Соколов сообщил: «я руководитель команды Альфа», люблю кофе",
            "люблю кофе",
        ),
    ],
)
async def test_sentence_narrator_cannot_authorize_attribution_or_extra_claim(
    sessions, original, entity
):
    ids, _, _ = await fixture_pair(
        sessions,
        old=original,
        retain=UNIT_PERSON_ORIGINAL,
        old_facts=[
            *NARRATOR_FACTS,
            {"entity": entity, "predicate": "роль", "value": "руководитель команды Альфа"},
        ],
        retain_facts=UNIT_PERSON_FACTS,
    )
    aid, result = prepare(sessions)
    assert aid is None and result.status == "needs_clarification"
    with sessions() as session:
        assert not session.scalar(select(Approval))
        assert all(session.get(MemoryEntry, entry_id) for entry_id in ids)


@pytest.mark.parametrize("selector", ["у меня двое детей", "люблю кофе", "отвечаю за закупки"])
async def test_sentence_selector_cannot_consume_independent_retained_claim(sessions, selector):
    ids, _, _ = await fixture_pair(
        sessions,
        old="Я руководитель команды Альфа",
        old_facts=[{"entity": "я", "predicate": "роль", "value": "руководитель команды Альфа"}],
        retain=f"Петров руководитель команды Альфа, {selector}",
        retain_facts=[
            {"entity": "Петров", "predicate": "роль", "value": "руководитель команды Альфа"}
        ],
    )
    aid, result = prepare(
        sessions,
        source=f"Актуальная версия — {selector}. Старую запись удали.",
        selector=selector,
    )
    assert aid is None and result.status == "needs_clarification"
    with sessions() as session:
        assert not session.scalar(select(Approval))
        assert all(session.get(MemoryEntry, entry_id) for entry_id in ids)


@pytest.mark.parametrize("name", ["Иван Соколов", "И. Соколов", "Анна-Мария Соколова"])
async def test_positive_person_narrator_normalization_keeps_full_approval_path(sessions, name):
    original = f"{name} сообщил: «я руководитель команды Альфа»"
    ids, _, _ = await fixture_pair(
        sessions,
        old=original,
        retain=UNIT_PERSON_ORIGINAL,
        old_facts=[
            {"entity": name, "predicate": "роль", "value": "руководитель команды Альфа"},
            {"entity": name, "predicate": "команда", "value": "Альфа"},
        ],
        retain_facts=UNIT_PERSON_FACTS,
    )
    aid, result = prepare(sessions)
    assert aid and original in result.user_message
    await callback(sessions, aid)
    await callback(sessions, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"
        assert not session.get(MemoryEntry, ids[0]) and session.get(MemoryEntry, ids[1])


@pytest.mark.parametrize("entities", [["Люблю кофе"], ["Люблю", "Кофе"]])
async def test_model_name_capitalization_does_not_consume_lowercase_independent_clause(
    sessions, entities
):
    ids, _, _ = await fixture_pair(
        sessions,
        old="Я руководитель команды Альфа, люблю кофе",
        old_facts=[
            {"entity": entity, "predicate": "роль", "value": "руководитель команды Альфа"}
            for entity in entities
        ],
    )
    aid, result = prepare(sessions)
    assert aid is None and result.status == "needs_clarification"
    with sessions() as session:
        assert not session.scalar(select(Approval))
        assert all(session.get(MemoryEntry, entry_id) for entry_id in ids)


@pytest.mark.parametrize(
    "original,entity",
    [
        ("Я руководитель команды Альфа. Люблю Кофе", "Люблю Кофе"),
        ("Я руководитель команды Альфа; Играю Шахматы", "Играю Шахматы"),
        ("Я руководитель команды Альфа, Я Люблю Кофе", "Люблю Кофе"),
        ("Я руководитель команды Альфа, Люблю Кофе", "Люблю Кофе"),
    ],
)
async def test_titlecase_entity_cannot_merge_separate_original_clauses(sessions, original, entity):
    ids, _, _ = await fixture_pair(
        sessions,
        old=original,
        old_facts=[{"entity": entity, "predicate": "роль", "value": "руководитель команды Альфа"}],
    )
    aid, result = prepare(sessions)
    assert aid is None and result.status == "needs_clarification"
    with sessions() as session:
        assert not session.scalar(select(Approval))
        assert all(session.get(MemoryEntry, entry_id) for entry_id in ids)


@pytest.mark.parametrize("entity", ["Люблю Кофе", "Играю Шахматы", "Люблю", "Неизвестное Имя"])
async def test_unknown_titlecase_narrator_is_not_positive_person_identity(sessions, entity):
    ids, _, _ = await fixture_pair(
        sessions,
        old=f"{entity} сообщил: «я руководитель команды Альфа»",
        retain=UNIT_PERSON_ORIGINAL,
        old_facts=[
            {"entity": entity, "predicate": "роль", "value": "руководитель команды Альфа"},
            {"entity": entity, "predicate": "команда", "value": "Альфа"},
        ],
        retain_facts=UNIT_PERSON_FACTS,
    )
    aid, result = prepare(sessions)
    assert aid is None and result.status == "needs_clarification"
    with sessions() as session:
        assert not session.scalar(select(Approval))
        assert all(session.get(MemoryEntry, entry_id) for entry_id in ids)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("entity", ["пользователь", "user"])
async def test_bounded_self_narrator_two_fact_projections_keep_actual_approval_path(
    sessions, legacy, entity
):
    ids, _, _ = await fixture_pair(
        sessions,
        legacy=legacy,
        old=f"{entity} сообщил: «я – руководитель команды Альфа»",
        retain=UNIT_PERSON_ORIGINAL,
        old_facts=[
            {"entity": entity, "predicate": "роль", "value": "руководитель команды Альфа"},
            {"entity": entity, "predicate": "команда", "value": "Альфа"},
        ],
        retain_facts=UNIT_PERSON_FACTS,
    )
    aid, result = prepare(sessions)
    assert aid and result.status == "ok"
    await callback(sessions, aid)
    await callback(sessions, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"
        assert not session.get(MemoryEntry, ids[0]) and session.get(MemoryEntry, ids[1])


async def test_sentence_containing_self_alias_is_not_self_subject(sessions):
    ids, _, _ = await fixture_pair(
        sessions,
        old="Пользователь любит кофе сообщил: «я руководитель команды Альфа»",
        old_facts=[
            {
                "entity": "пользователь любит кофе",
                "predicate": "роль",
                "value": "руководитель команды Альфа",
            }
        ],
    )
    aid, result = prepare(sessions)
    assert aid is None and result.status == "needs_clarification"
    with sessions() as session:
        assert not session.scalar(select(Approval))
        assert all(session.get(MemoryEntry, entry_id) for entry_id in ids)

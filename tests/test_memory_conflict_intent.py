"""Delivered conflict references through real ingress, runtime and approval storage."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import api
from app.config import Settings
from app.data_controls import PrepareDataDeletion
from app.domain_actions import Approval, CreateTask, PrepareMeeting, handle_callback
from app.domain_memory import (
    Entity,
    Fact,
    MemoryEntry,
    SaveArgs,
    SaveMemory,
    SearchDocument,
    SearchMemory,
)
from app.memory_output import validate_memory_output
from app.memory_resolution import (
    PrepareMemoryResolution,
    ShownResolutionArgs,
    bind_shown_delivery,
    digest,
    latest_shown_context,
    pair_records,
    prepare_shown_resolution,
)
from app.models import (
    ApprovalAudit,
    ApprovalPreview,
    History,
    Invocation,
    Job,
    MemoryContext,
    Operation,
    Outbox,
    UserState,
    now,
)
from app.providers import BudgetExceeded, YandexProvider, wire_bytes
from app.queue import claim, enqueue_text
from app.runtime import Runtime, project_result
from app.tools import Registry
from tests.test_memory_documents import Embeddings
from tests.test_memory_resolution import (
    UNIT_PERSON_FACTS,
    UNIT_PERSON_ORIGINAL,
    durable_context,
    fixture_pair,
)

SELECTION_SOURCE = "Актуальная версия — Лёша. Другую удали."


def registry(sessions, provider, cfg):
    result = Registry()
    for name, handler in {
        "create_task": CreateTask(),
        "prepare_meeting": PrepareMeeting(),
        "save_memory": SaveMemory(sessions, provider),
        "search_memory": SearchMemory(sessions, provider),
        "search_document": SearchDocument(sessions, provider),
        "prepare_data_deletion": PrepareDataDeletion(cfg.file_directory),
        "prepare_memory_resolution": PrepareMemoryResolution(),
    }.items():
        result.register(name, handler)
    return result


def ingress(sessions, monkeypatch, source, owner=42, chat=42, expected_kind="agent"):
    cfg = Settings(service_token="test", allowed_telegram_user_id=owner)
    monkeypatch.setattr(api, "settings", lambda: cfg)
    monkeypatch.setattr(api, "session_factory", lambda: sessions)
    update_id = uuid.uuid4().int % 900000000000000000
    payload = {
        "update_id": update_id,
        "message": {
            "from": {"id": owner},
            "chat": {"id": chat},
            "text": source,
            "date": int(now().timestamp()),
        },
    }
    response = TestClient(api.app).post(
        "/internal/updates", json=payload, headers={"Authorization": "Bearer test"}
    )
    assert response.status_code == 200
    with sessions() as session:
        op = session.scalar(select(Operation).where(Operation.update_id == update_id))
        job = session.scalar(select(Job).where(Job.operation_id == op.id))
        assert job.kind == expected_kind
        return op.id


async def run(sessions, op_id, protocol="native", call=None, final="Проверьте две версии."):
    requests = []
    cfg = Settings(
        ai_api_key="test",
        ai_folder_id="folder",
        ai_attempts=1,
        tool_protocol=protocol,
        pricing_path=Path("config/pricing.json"),
    )

    def respond(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": [1.0] + [0.0] * 255}],
                    "usage": {"prompt_tokens": 1},
                    "model": payload["model"],
                },
            )
        requests.append(payload)
        if len(requests) == 1 and call:
            name, args = call(payload) if callable(call) else call
            if protocol == "native":
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                    ],
                }
            else:
                message = {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "type": "tool",
                            "name": name,
                            "arguments": args,
                        },
                        ensure_ascii=False,
                    ),
                }
        else:
            message = {
                "role": "assistant",
                "content": final
                if protocol == "native"
                else json.dumps({"type": "final", "text": final}, ensure_ascii=False),
            }
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = YandexProvider(cfg, sessions, httpx.MockTransport(respond))
    await Runtime(sessions, provider, registry(sessions, provider, cfg), protocol).run(op_id)
    return requests


async def owner_callback(sessions, monkeypatch, approval_id, choice="y"):
    cfg = Settings(service_token="test", allowed_telegram_user_id=42)
    monkeypatch.setattr(api, "settings", lambda: cfg)
    monkeypatch.setattr(api, "session_factory", lambda: sessions)
    update_id = uuid.uuid4().int % 900000000000000000
    payload = {
        "update_id": update_id,
        "callback_query": {
            "id": str(uuid.uuid4()),
            "from": {"id": 42},
            "message": {"chat": {"id": 42}, "message_id": 100},
            "data": f"a:{approval_id}:{choice}",
        },
    }
    response = TestClient(api.app).post(
        "/internal/updates", json=payload, headers={"Authorization": "Bearer test"}
    )
    assert response.status_code == 200
    with sessions.begin() as session:
        job = claim(session, Job, 42, include_kinds={"callback"})
        query = job.payload["callback_query"]
        assert query["id"] == payload["callback_query"]["id"]
        assert query["data"] == payload["callback_query"]["data"]
        assert query["from"]["id"] == 42 and query["message"]["chat"]["id"] == 42
    await handle_callback(sessions, job, (job.id, job.lease_token))
    with sessions.begin() as session:
        session.get(Job, job.id).status = "done"


def ack(sessions, op_id):
    with sessions.begin() as session:
        for row in session.scalars(select(Outbox).where(Outbox.operation_id == op_id)):
            row.status, row.acknowledged_at = "done", now()


async def seed(sessions, names=("Лёша", "Саша"), extra=True, original_suffix="", extra_fact=None):
    ids = []
    values = [f"{name} руководитель команды Сьерра" for name in names]
    if extra:
        values.append("Люблю кофе")
    for index, value in enumerate(values):
        ctx = durable_context(sessions, "Запомни: " + value)
        facts = (
            [{"entity": "команда Сьерра", "predicate": "руководитель", "value": names[index]}]
            if index < len(names)
            else [{"entity": "я", "predicate": "напиток", "value": "кофе"}]
        )
        if extra_fact and index == 0:
            facts.append(extra_fact)
        args = SaveArgs(text=value + (original_suffix if index == 0 else ""), facts=facts)
        handler = SaveMemory(sessions, Embeddings())
        ready = await handler.prepare(ctx, args)
        with sessions.begin() as session:
            result = handler.apply(session, ctx, args, ready)
            inv = session.get(Invocation, uuid.UUID(ctx.idempotency_key))
            inv.arguments, inv.result = args.model_dump(), result.model_dump()
        ids.append(uuid.UUID(result.data["entry_id"]))
    return ids


async def show(sessions, monkeypatch, protocol="native"):
    op_id = ingress(sessions, monkeypatch, "Кто руководитель Сьерры?")
    requests = await run(
        sessions, op_id, protocol, ("search_memory", {"query": "руководитель Сьерра"})
    )
    ack(sessions, op_id)
    with sessions() as session:
        context = session.scalar(select(MemoryContext).where(MemoryContext.operation_id == op_id))
        return op_id, context.shown_conflicts, requests


def select_direction(target):
    def call(payload):
        messages = payload["messages"]
        shown = next(
            json.loads(m["content"].split(": ", 1)[1])
            for m in messages
            if m.get("content", "").startswith("Доставленная пара")
        )
        pair = shown[0]
        retain = next(k for k in ("a", "b") if target in pair[k])
        return "prepare_memory_resolution", {
            "conflict_ref": pair["conflict_ref"],
            "retain_ref": retain,
        }

    return call


def visible(sessions, op_id=None):
    with sessions() as session:
        rows = session.scalars(select(Outbox).where(Outbox.kind == "sendMessage"))
        return "\n".join(
            r.payload.get("text", "") for r in rows if not op_id or r.operation_id == op_id
        )


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_direct_semantic_reference_exact_pair_restart_cancel(sessions, monkeypatch, protocol):
    source = SELECTION_SOURCE
    ids = await seed(sessions)
    shown_op, contract, _ = await show(sessions, monkeypatch, protocol)
    assert len(contract["pairs"]) == 1
    assert {contract["pairs"][0][k] for k in ("a", "b")} == {str(i) for i in ids[:2]}
    assert ids[2] and len(contract["delivery"]["keys"]) >= 1
    assert "Лёша руководитель" in visible(sessions, shown_op)
    assert "Саша руководитель" in visible(sessions, shown_op)
    op_id = ingress(sessions, monkeypatch, source)
    requests = await run(
        sessions,
        op_id,
        protocol,
        select_direction("Лёша"),
        "Подтвердите или отмените удаление старой версии в карточке.",
    )
    assert len(requests) == 1
    first = requests[0]
    schemas = (
        first["tools"]
        if protocol == "native"
        else json.loads(first["messages"][0]["content"])["tools"]
    )
    assert len(schemas) == 7
    assert sum(wire_bytes(r) for r in requests) <= 16000
    with sessions() as session:
        row = session.scalar(select(Approval).where(Approval.operation_id == op_id))
        assert row.action_kind == "memory_resolution_shown" and row.payload["schema"] == 2
        assert row.payload["retain_entry_id"] == str(ids[0])
        assert row.payload["old_entry_id"] == str(ids[1])
        assert row.payload["shown"] == contract
        assert row.payload["hash"] == digest({k: v for k, v in row.payload.items() if k != "hash"})
        assert abs((row.expires_at - now()).total_seconds() - 86400) < 30
        assert all(session.get(MemoryEntry, i) for i in ids)
        card = session.get(ApprovalPreview, row.id).text
        assert "Сохранить актуальную запись:\nЛёша" in card
        assert "Удалить старую запись:\nСаша" in card
        aid = row.id
    validate_memory_output(visible(sessions))
    await owner_callback(sessions, monkeypatch, aid, choice="n")
    await owner_callback(sessions, monkeypatch, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "cancelled"
        assert all(session.get(MemoryEntry, i) for i in ids)


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("target", ["Лёша", "Саша", "Элиан", "Зорвин"])
async def test_confirm_repeat_retrieval_one_arbitrary_names(
    sessions, monkeypatch, protocol, target
):
    names = ("Элиан", "Зорвин") if target in {"Элиан", "Зорвин"} else ("Лёша", "Саша")
    ids = await seed(sessions, names)
    await show(sessions, monkeypatch, protocol)
    op_id = ingress(sessions, monkeypatch, f"{target} верен, вторую версию удали.")
    await run(
        sessions, op_id, protocol, select_direction(target), "Удаление ожидает подтверждения."
    )
    with sessions() as session:
        row = session.scalar(select(Approval).where(Approval.operation_id == op_id))
        assert row and row.payload["retain_entry_id"] == str(ids[names.index(target)])
        aid = row.id
    await owner_callback(sessions, monkeypatch, aid)
    await owner_callback(sessions, monkeypatch, aid)
    query = ingress(sessions, monkeypatch, "Кто теперь руководитель команды Сьерра?")
    await run(
        sessions,
        query,
        protocol,
        ("search_memory", {"query": "команда Сьерра"}),
        f"Руководитель команды Сьерра — {target}.",
    )
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"
        assert session.get(MemoryEntry, ids[names.index(target)])
        assert not session.get(MemoryEntry, ids[1 - names.index(target)])
        assert session.get(MemoryEntry, ids[2])
        context = session.scalar(select(MemoryContext).where(MemoryContext.operation_id == query))
        assert context.shown_conflicts["pairs"] == []
        assert len(session.scalars(select(Fact).where(Fact.entry_id.in_(ids[:2]))).all()) == 1
    validate_memory_output(visible(sessions))


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("entity", ["Соколов", "пользователь", "user", "я"])
async def test_legacy_cross_self_no_structured_conflicts_runtime_flow(
    sessions, monkeypatch, protocol, entity
):
    old = (
        f"{entity} сообщил: «я – руководитель команды Альфа»"
        if entity != "я"
        else "Я руководитель команды Альфа"
    )
    facts = [
        {"entity": entity, "predicate": "роль", "value": "руководитель команды Альфа"},
        {"entity": entity, "predicate": "команда", "value": "Альфа"},
    ]
    ids, _, _ = await fixture_pair(
        sessions,
        old=old,
        retain=UNIT_PERSON_ORIGINAL,
        old_facts=facts,
        retain_facts=UNIT_PERSON_FACTS,
    )
    query = ingress(sessions, monkeypatch, "Кто руководит Альфой?")
    await run(sessions, query, protocol, ("search_memory", {"query": "Альфа"}))
    ack(sessions, query)
    with sessions() as session:
        inv = session.scalar(select(Invocation).where(Invocation.operation_id == query))
        assert inv.result["data"]["conflicts"] == []
        context = session.scalar(select(MemoryContext).where(MemoryContext.operation_id == query))
        assert context.shown_conflicts["pairs"][0]["validator"]["kind"] == "legacy"
        assert old in visible(sessions, query) and UNIT_PERSON_ORIGINAL in visible(sessions, query)
    op_id = ingress(sessions, monkeypatch, "Правильная версия про Петрова, другую удали.")
    await run(sessions, op_id, protocol, select_direction("Петров"), "Подтвердите действие.")
    with sessions() as session:
        row = session.scalar(select(Approval).where(Approval.operation_id == op_id))
        assert row and row.payload["old_entry_id"] == str(ids[0])
        aid = row.id
    await owner_callback(sessions, monkeypatch, aid)
    await owner_callback(sessions, monkeypatch, aid)
    query2 = ingress(sessions, monkeypatch, "Кто руководит Альфой?")
    await run(
        sessions,
        query2,
        protocol,
        ("search_memory", {"query": "Альфа"}),
        "Руководитель команды Альфа — Петров.",
    )
    with sessions() as session:
        assert not session.get(MemoryEntry, ids[0]) and session.get(MemoryEntry, ids[1])
        inv = session.scalar(select(Invocation).where(Invocation.operation_id == query2))
        assert len(inv.result["data"]["memory"]) == 1
    validate_memory_output(visible(sessions))


@pytest.mark.parametrize("suffix", [", люблю кофе", "; у меня двое детей", ". Мне 37 лет."])
async def test_structured_slot_never_consumes_independent_original(sessions, monkeypatch, suffix):
    await seed(sessions, original_suffix=suffix)
    _, contract, _ = await show(sessions, monkeypatch)
    assert contract["pairs"] == []


async def test_structured_slot_never_consumes_extra_fact(sessions, monkeypatch):
    await seed(sessions, extra_fact={"entity": "Лёша", "predicate": "дети", "value": "двое"})
    _, contract, _ = await show(sessions, monkeypatch)
    assert contract["pairs"] == []


async def test_three_versions_never_pick_arbitrary_pair(sessions, monkeypatch):
    await seed(sessions, names=("Лёша", "Саша", "Элиан"))
    _, contract, _ = await show(sessions, monkeypatch)
    assert contract["pairs"] == []


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_main_agent_final_without_tool_keeps_memory_unchanged(
    sessions, monkeypatch, protocol
):
    # The model decision is fixed by the fixture; this verifies the no-tool execution path.
    source = "Не удаляй Сашу, оставь обе записи."
    await seed(sessions)
    await show(sessions, monkeypatch, protocol)
    op_id = ingress(sessions, monkeypatch, source)
    requests = await run(sessions, op_id, protocol, final="Уточните, какую запись нужно удалить.")
    assert len(requests) == 1
    with sessions() as session:
        assert not session.scalar(select(Approval))
        assert len(session.scalars(select(MemoryEntry)).all()) == 3


@pytest.mark.parametrize(
    "failure",
    [
        "private",
        "partial",
        "revision",
        "epoch",
        "snapshot",
        "foreign_chat",
        "foreign_owner",
        "foreign_ref",
        "later",
    ],
)
async def test_exact_delivery_boundary_fails_closed(sessions, monkeypatch, failure):
    ids = await seed(sessions)
    shown_op, contract, _ = await show(sessions, monkeypatch)
    pair = contract["pairs"][0]
    op_id = ingress(
        sessions,
        monkeypatch,
        "Оставь Лёшу, вторую версию удали.",
        owner=99 if failure == "foreign_owner" else 42,
        chat=99 if failure == "foreign_chat" else 42,
    )
    with sessions.begin() as session:
        context = session.scalar(
            select(MemoryContext).where(MemoryContext.operation_id == shown_op)
        )
        origin = session.get(Operation, shown_op)
        if failure == "private":
            context.shown_conflicts = {**context.shown_conflicts, "delivery": None}
        elif failure == "partial":
            row = session.scalar(select(Outbox).where(Outbox.operation_id == shown_op))
            row.status, row.acknowledged_at = "pending", None
        elif failure == "revision":
            origin.terminal_revision += 1
        elif failure == "epoch":
            session.get(UserState, 42).context_epoch += 1
        elif failure == "snapshot":
            session.get(MemoryEntry, ids[0]).original += "!"
    if failure == "later":
        later = ingress(sessions, monkeypatch, "Что помнишь про кофе?")
        await run(sessions, later, call=("search_memory", {"query": "кофе", "entity": "я"}))
        ack(sessions, later)
    with sessions.begin() as session:
        op = session.get(Operation, op_id)
        inv = Invocation(
            operation_id=op_id, call_id="test", name="prepare_memory_resolution", arguments={}
        )
        session.add(inv)
        session.flush()
        result = prepare_shown_resolution(
            session,
            op,
            inv.id,
            ShownResolutionArgs(
                conflict_ref="unknown" if failure == "foreign_ref" else pair["conflict_ref"],
                retain_ref="a",
            ),
        )
        assert result.status == "needs_clarification"
        assert not session.scalar(select(Approval))
        assert all(session.get(MemoryEntry, i) for i in ids)


@pytest.mark.parametrize("mutation", ["original", "fact", "embedding", "revision", "expiry"])
async def test_schema2_confirm_stale_or_expired_no_delete(sessions, monkeypatch, mutation):
    ids = await seed(sessions)
    origin, _, _ = await show(sessions, monkeypatch)
    op_id = ingress(sessions, monkeypatch, SELECTION_SOURCE)
    await run(sessions, op_id, call=select_direction("Лёша"), final="Подтвердите действие.")
    with sessions.begin() as session:
        row = session.scalar(select(Approval).where(Approval.operation_id == op_id))
        aid = row.id
        if mutation == "original":
            session.get(MemoryEntry, ids[0]).original += "!"
        elif mutation == "fact":
            session.scalar(select(Fact).where(Fact.entry_id == ids[1])).value = "Изменено"
        elif mutation == "embedding":
            session.get(MemoryEntry, ids[1]).embedding = [0.5] + [0.0] * 255
        elif mutation == "revision":
            session.get(Operation, origin).terminal_revision += 1
        else:
            row.expires_at = now() - timedelta(seconds=1)
    await owner_callback(sessions, monkeypatch, aid)
    with sessions() as session:
        row = session.get(Approval, aid)
        assert row.status == ("expired" if mutation == "expiry" else "stale")
        assert all(session.get(MemoryEntry, i) for i in ids)


INTERNAL_OUTPUT_CASES = [
    "01234567-89ab-cdef-0123-456789abcdef",
    "mc_0123456789ab",
    'Поиск: {"entry_id":"secret"}',
    "Противоречащие записи памяти (обе версии сохранены):",
    '{"callback_data":"a:fake:y"}',
    '{"type":"tool","name":"search_memory"}',
]


@pytest.mark.parametrize("text_value", INTERNAL_OUTPUT_CASES)
def test_internal_output_forms_rejected(text_value):
    from app.terminal import InvalidFinal

    with pytest.raises(InvalidFinal):
        validate_memory_output(text_value)


@pytest.mark.parametrize("phase", ["ordinary", "search", "save", "card"])
async def test_unsafe_model_output_never_visible(sessions, monkeypatch, phase):
    text_value = "mc_0123456789ab"
    await seed(sessions)
    if phase == "card":
        await show(sessions, monkeypatch)
    op_id = ingress(sessions, monkeypatch, SELECTION_SOURCE if phase == "card" else "Что помнишь?")
    calls = {
        "ordinary": None,
        "search": ("search_memory", {"query": "Сьерра"}),
        "save": ("save_memory", {"text": "Я люблю чай", "facts": []}),
        "card": select_direction("Лёша"),
    }
    await run(sessions, op_id, call=calls[phase], final=text_value)
    with sessions() as session:
        op = session.get(Operation, op_id)
        assert op.status == ("done" if phase == "card" else "error")
        assert op.error_reason == (None if phase == "card" else "invalid_final")
        finals = [
            h.message.get("content", "")
            for h in session.scalars(select(History).where(History.operation_id == op_id))
            if h.message.get("role") == "assistant" and not h.message.get("tool_calls")
        ]
        assert text_value not in finals
    assert text_value not in visible(sessions, op_id)


async def test_card_contradiction_rejected(sessions, monkeypatch):
    await seed(sessions)
    await show(sessions, monkeypatch)
    op_id = ingress(sessions, monkeypatch, SELECTION_SOURCE)
    await run(sessions, op_id, call=select_direction("Лёша"), final="Я уже удалил Сашу.")
    with sessions() as session:
        assert session.get(Operation, op_id).status == "done"
        assert session.get(Operation, op_id).error_reason is None
        assert session.scalar(select(Approval)).status == "pending"
        assert len(session.scalars(select(MemoryEntry)).all()) == 3
    assert "Я уже удалил" not in visible(sessions, op_id)


def test_memory_projection_preserves_complete_original_refs_or_fails():
    pair = {"conflict_ref": "mc_0123456789ab", "a": "А" * 3000, "b": "Б" * 3000}
    result = {"status": "ok", "data": {"shown_pairs": [pair], "memory": [], "facts": []}}
    projected = project_result(result, lambda p: True, "search_memory")
    assert projected["data"]["shown_pairs"] == [pair]
    with pytest.raises(BudgetExceeded):
        project_result(result, lambda p: False, "search_memory")


async def test_partial_multi_chunk_delivery_requires_every_chunk(sessions, monkeypatch):
    await seed(sessions)
    op_id, contract, _ = await show(sessions, monkeypatch)
    with sessions.begin() as session:
        context = session.scalar(select(MemoryContext).where(MemoryContext.operation_id == op_id))
        op = session.get(Operation, op_id)
        records = pair_records(session, context)
        context.shown_conflicts = {**context.shown_conflicts, "delivery": None}
        session.query(Outbox).filter(Outbox.operation_id == op_id).delete()
        body = "Варианты. " * 700 + records[0]["a"] + "\n" + records[0]["b"]
        enqueue_text(session, op, body, key_prefix=f"{op_id}:long")
        bind_shown_delivery(session, context, op, f"{op_id}:long")
        session.flush()
        rows = session.scalars(select(Outbox).where(Outbox.operation_id == op_id)).all()
        assert len(rows) >= 2
        rows[0].status, rows[0].acknowledged_at = "done", now()
    next_id = ingress(sessions, monkeypatch, SELECTION_SOURCE)
    with sessions() as session:
        assert latest_shown_context(session, session.get(Operation, next_id)) is None
    ack(sessions, op_id)
    with sessions() as session:
        assert latest_shown_context(session, session.get(Operation, next_id))


async def test_old_source_dispatcher_barrier_restore_new_executor(sessions, monkeypatch, tmp_path):
    ids = await seed(sessions)
    await show(sessions, monkeypatch)
    op_id = ingress(sessions, monkeypatch, SELECTION_SOURCE)
    await run(sessions, op_id, call=select_direction("Лёша"), final="Подтвердите действие.")
    with sessions() as session:
        row = session.scalar(select(Approval).where(Approval.operation_id == op_id))
        aid, payload, preview = row.id, row.payload, session.get(ApprovalPreview, row.id).text
    base = "645b6dcde043fa3d19a6576a199588e605201147"
    archive = subprocess.run(
        ["git", "archive", base, "app", "config"], capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp_path, filter="data")
    old_code = (tmp_path / "app/domain_actions.py").read_bytes()
    assert hashlib.sha256(old_code).hexdigest() == (
        "093ee7d71a84a6137b27d9b82f241fb57b5c6d413d794566485747cd8e51eba8"
    )
    script = """import asyncio,sys,uuid
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from app.config import Settings
Settings.model_config["env_file"]=None
from app.domain_actions import handle_callback
from app.models import Job,Operation,UserState
sessions=sessionmaker(create_engine(sys.argv[1]),expire_on_commit=False)
with sessions.begin() as s:
    state=s.get(UserState,42)
    op=Operation(owner_id=42,chat_id=42,timezone="Europe/Moscow",context_epoch=state.context_epoch)
    s.add(op);s.flush()
    job=Job(key="old-source-callback",operation_id=op.id,kind="callback",payload={
        "callback_query":{"id":"old-source-query","from":{"id":42},
                          "message":{"chat":{"id":42}},"data":"a:"+sys.argv[2]+":y"}})
    s.add(job);s.flush()
asyncio.run(handle_callback(sessions,job,None))
with sessions.begin() as s:
    s.get(Job,job.id).status="done"
"""
    result = subprocess.run(
        [sys.executable, "-c", script, os.environ["TEST_DATABASE_URL"], str(aid)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    with sessions() as session:
        row = session.get(Approval, aid)
        assert row.status == "pending" and row.payload == payload
        assert session.get(ApprovalPreview, aid).text == preview
        assert all(session.get(MemoryEntry, i) for i in ids)
        assert session.get(ApprovalAudit, "old-source-query").outcome == "unavailable"
    await owner_callback(sessions, monkeypatch, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"
        assert not session.get(MemoryEntry, ids[1]) and session.get(MemoryEntry, ids[0])


async def test_nullable_migration6_upgrade_downgrade_preserves_schema1(sessions, monkeypatch):
    from tests.test_memory_resolution import prepare

    await fixture_pair(sessions)
    aid, _ = prepare(sessions)
    engine = sessions.kw["bind"]
    with sessions() as session:
        payload = session.get(Approval, aid).payload
        preview = session.get(ApprovalPreview, aid).text
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE memory_contexts DROP COLUMN shown_conflicts"))
        connection.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) PRIMARY KEY)")
        )
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0005')"))
    cfg = Config("alembic.ini")
    monkeypatch.setattr(
        "app.config.settings", lambda: Settings(database_url=os.environ["TEST_DATABASE_URL"])
    )
    command.upgrade(cfg, "0006")
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
        assert (
            connection.scalar(
                text("SELECT count(*) FROM memory_contexts WHERE shown_conflicts IS NOT NULL")
            )
            == 0
        )
    command.downgrade(cfg, "0005")
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
    command.upgrade(cfg, "0006")
    with sessions() as session:
        assert session.get(Approval, aid).payload == payload
        assert session.get(ApprovalPreview, aid).text == preview
    await owner_callback(sessions, monkeypatch, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"


async def test_clean_migration_upgrade6_downgrade5(sessions, monkeypatch):
    from app.db import Base

    engine = sessions.kw["bind"]
    with engine.begin() as connection:
        Base.metadata.drop_all(connection)
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        connection.execute(text("DROP FUNCTION IF EXISTS protect_approval_payload()"))
        connection.execute(text("DROP FUNCTION IF EXISTS protect_approval_preview()"))
    monkeypatch.setattr(
        "app.config.settings", lambda: Settings(database_url=os.environ["TEST_DATABASE_URL"])
    )
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
        assert (
            connection.scalar(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name='memory_contexts' AND column_name='shown_conflicts'"
                )
            )
            == "YES"
        )
    command.downgrade(cfg, "0005")
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='memory_contexts' AND column_name='shown_conflicts'"
                )
            )
            == 0
        )
    command.upgrade(cfg, "head")


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_long_history_keeps_full_shown_pair_all_tools_and_budget(
    sessions, monkeypatch, protocol
):
    await seed(sessions)
    shown_op, contract, _ = await show(sessions, monkeypatch, protocol)
    # Unrelated history cannot displace the authoritative delivered pair.
    from tests.test_storage import operation

    history_id = operation(sessions)
    with sessions.begin() as session:
        op = session.get(Operation, history_id)
        op.status = "done"
        session.add(
            History(
                operation_id=history_id,
                owner_id=42,
                message={"role": "user", "content": "Большая история. " * 2000},
            )
        )
    op_id = ingress(sessions, monkeypatch, SELECTION_SOURCE)
    requests = await run(
        sessions, op_id, protocol, select_direction("Лёша"), "Подтвердите удаление старой версии."
    )
    assert len(requests) == 1 and sum(wire_bytes(p) for p in requests) <= 16000
    schemas = (
        requests[0]["tools"]
        if protocol == "native"
        else json.loads(requests[0]["messages"][0]["content"])["tools"]
    )
    assert len(schemas) == 7
    assert "Большая история" not in json.dumps(requests[0], ensure_ascii=False)
    with sessions() as session:
        row = session.scalar(select(Approval).where(Approval.operation_id == op_id))
        assert row.payload["shown"] == contract
        assert row.payload["origin_operation_id"] == str(shown_op)


@pytest.mark.parametrize(
    "kind,schema",
    [
        ("memory_resolution_shown", 1),
        ("memory_resolution", 2),
        ("memory_resolution_shown", 3),
        ("unknown_memory_resolution", 2),
    ],
)
async def test_unknown_mismatched_kind_schema_dispatcher_executor_fail_closed(
    sessions, monkeypatch, kind, schema
):
    ids = await seed(sessions)
    await show(sessions, monkeypatch)
    op_id = ingress(sessions, monkeypatch, SELECTION_SOURCE)
    await run(sessions, op_id, call=select_direction("Лёша"), final="Подтвердите действие.")
    with sessions.begin() as session:
        original = session.scalar(select(Approval).where(Approval.operation_id == op_id))
        inv = Invocation(
            operation_id=op_id,
            call_id="incompatible",
            name="prepare_memory_resolution",
            arguments={},
        )
        session.add(inv)
        session.flush()
        payload = {**original.payload, "schema": schema}
        payload["hash"] = digest({k: v for k, v in payload.items() if k != "hash"})
        card = Approval(
            invocation_id=inv.id,
            operation_id=op_id,
            owner_id=42,
            chat_id=42,
            action_kind=kind,
            payload=payload,
            expires_at=now() + timedelta(hours=24),
        )
        session.add(card)
        session.flush()
        aid = card.id
        with pytest.raises(ValueError, match="invalid_resolution_version"):
            from app.memory_resolution import execute_resolution

            execute_resolution(session, card)
    await owner_callback(sessions, monkeypatch, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "pending"
        assert all(session.get(MemoryEntry, i) for i in ids)
        assert session.scalar(select(ApprovalAudit).where(ApprovalAudit.outcome == "unavailable"))


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_legacy_pending_invocation_resumes_schema1_without_rewriting_card(
    sessions, monkeypatch, protocol
):
    from tests.test_memory_resolution import SOURCE

    await fixture_pair(sessions)
    op_id = ingress(sessions, monkeypatch, SOURCE)
    with sessions.begin() as session:
        inv = Invocation(
            operation_id=op_id,
            call_id="historical-pending",
            name="prepare_memory_resolution",
            arguments={"selector": "Петров"},
        )
        session.add(inv)
    requests = await run(sessions, op_id, protocol, final="Подтвердите действие.")
    assert len(requests) == 0
    with sessions() as session:
        row = session.scalar(select(Approval).where(Approval.operation_id == op_id))
        assert row.action_kind == "memory_resolution" and row.payload["schema"] == 1
        aid, payload = row.id, row.payload
        preview = session.get(ApprovalPreview, aid).text
    await run(sessions, op_id, protocol, final="Повтор")
    with sessions() as session:
        assert session.get(Approval, aid).payload == payload
        assert session.get(ApprovalPreview, aid).text == preview
    await owner_callback(sessions, monkeypatch, aid)
    with sessions() as session:
        assert session.get(Approval, aid).status == "executed"


async def test_new_invalid_selector_never_resumes_as_schema1(sessions, monkeypatch):
    await fixture_pair(sessions)
    op_id = ingress(sessions, monkeypatch, "Актуальная версия — Петров. Старую запись удали.")
    await run(
        sessions,
        op_id,
        call=("prepare_memory_resolution", {"selector": "Петров"}),
        final="Уточните выбор версии.",
    )
    with sessions.begin() as session:
        inv = session.scalar(select(Invocation).where(Invocation.operation_id == op_id))
        assert "selector" not in inv.arguments
        assert inv.result["status"] == "error"
        inv.result, inv.model_result = None, None
        session.get(Operation, op_id).status = "running"
    await run(sessions, op_id, final="Уточните выбор версии.")
    with sessions() as session:
        assert not session.scalar(select(Approval))


async def test_schema2_cleanup_shown_provenance_late_copy_and_unrelated(sessions, monkeypatch):
    ids = await seed(sessions)
    shown_op, _, _ = await show(sessions, monkeypatch)
    op_id = ingress(sessions, monkeypatch, SELECTION_SOURCE)
    await run(sessions, op_id, call=select_direction("Лёша"), final="Подтвердите действие.")
    with sessions() as session:
        aid = session.scalar(select(Approval).where(Approval.operation_id == op_id)).id
        unrelated = session.get(MemoryEntry, ids[2])
        unrelated_snapshot = (unrelated.original, unrelated.source_text, list(unrelated.embedding))
        card = session.get(ApprovalPreview, aid).text
        validate_memory_output(card)
        assert "Лёша" in card and "Саша" in card
    # A derivative produced after preparation must still enter the targeted cleanup closure.
    later, _, _ = await show(sessions, monkeypatch)
    await owner_callback(sessions, monkeypatch, aid)
    with sessions() as session:
        assert not session.scalar(
            select(MemoryContext).where(MemoryContext.operation_id.in_([shown_op, later]))
        )
        for copy_op in (shown_op, later):
            assert not session.scalar(select(History).where(History.operation_id == copy_op))
            assert all(
                not out.payload
                for out in session.scalars(select(Outbox).where(Outbox.operation_id == copy_op))
            )
        unrelated = session.get(MemoryEntry, ids[2])
        assert (
            unrelated.original,
            unrelated.source_text,
            list(unrelated.embedding),
        ) == unrelated_snapshot
        assert session.get(MemoryEntry, ids[0]) and not session.get(MemoryEntry, ids[1])


@pytest.mark.parametrize("mutation", ["original", "fact"])
async def test_retrieval_apply_gap_never_certifies_new_snapshot(sessions, mutation):
    from app.domain_memory import SearchArgs
    from app.privacy import ContextChanged

    ids = await seed(sessions)
    ctx = durable_context(sessions, "Кто руководитель Сьерры?", "search_memory")
    handler = SearchMemory(sessions, Embeddings())
    args = SearchArgs(query="Сьерра")
    prepared = await handler.prepare(ctx, args)
    with sessions.begin() as session:
        if mutation == "original":
            session.get(MemoryEntry, ids[0]).original += "!"
        else:
            session.scalar(select(Fact).where(Fact.entry_id == ids[0])).value = "Изменено"
    with sessions.begin() as session:
        with pytest.raises(ContextChanged):
            handler.apply(session, ctx, args, prepared)
        assert not session.scalar(select(MemoryContext))


@pytest.mark.parametrize(
    "entity,original",
    [
        ("Люблю кофе", "Я руководитель команды Альфа, люблю кофе"),
        (
            "пользователь любит кофе",
            "Пользователь любит кофе сообщил: «я руководитель команды Альфа»",
        ),
    ],
)
async def test_legacy_sentence_entity_never_becomes_shown_candidate(sessions, entity, original):
    ids, _, retrieval = await fixture_pair(
        sessions,
        old=original,
        retain=UNIT_PERSON_ORIGINAL,
        old_facts=[{"entity": entity, "predicate": "роль", "value": "руководитель команды Альфа"}],
        retain_facts=UNIT_PERSON_FACTS,
    )
    with sessions() as session:
        context = session.scalar(
            select(MemoryContext).where(MemoryContext.operation_id == retrieval.operation_id)
        )
        assert context.shown_conflicts["pairs"] == []
        assert all(session.get(MemoryEntry, entry_id) for entry_id in ids)


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("orientation", ["person", "mixed", "split", "split_genitive"])
@pytest.mark.parametrize(
    "names,finish", [(("Лёша", "Саша"), "confirm"), (("Элиан", "Зорвин"), "cancel")]
)
async def test_model_person_role_unit_save_question_selection_full_originals(
    sessions, monkeypatch, protocol, orientation, names, finish
):
    ids, originals = [], []
    for name in (*names, None):
        original = f"{name} руководитель команды Сьерра." if name else "Я люблю кофе."
        facts = (
            [{"entity": name, "predicate": "руководитель команды", "value": "Сьерра"}]
            if name
            else [{"entity": "я", "predicate": "напиток", "value": "кофе"}]
        )
        if orientation == "mixed" and name == names[0]:
            facts = [{"entity": "Сьерра", "predicate": "руководитель", "value": name}]
        elif orientation.startswith("split") and name == names[1]:
            unit = "команды" if orientation == "split_genitive" else "команда"
            facts = [{"entity": name, "predicate": "руководитель", "value": unit + " Сьерра"}]
        op_id = ingress(sessions, monkeypatch, "Запомни: " + original)
        requests = await run(
            sessions,
            op_id,
            protocol,
            (
                "save_memory",
                {
                    "text": original,
                    "facts": facts,
                },
            ),
            "Запомнил факт.",
        )
        assert len(requests) == 2 and sum(wire_bytes(p) for p in requests) <= 16000
        ack(sessions, op_id)
        with sessions() as session:
            entry = session.scalar(
                select(MemoryEntry).join(Invocation).where(Invocation.operation_id == op_id)
            )
            assert entry and entry.original == original
            ids.append(entry.id)
            originals.append(entry.original)
    query, contract, requests = await show(sessions, monkeypatch, protocol)
    with sessions() as session:
        inv = session.scalar(select(Invocation).where(Invocation.operation_id == query))
        assert inv.result["data"]["conflicts"] == []
        assert len(inv.result["data"]["memory"]) == 3
        assert len(contract["pairs"]) == 1
        assert contract["pairs"][0]["validator"]["kind"] == "projection"
        assert {contract["pairs"][0][k] for k in ("a", "b")} == {str(i) for i in ids[:2]}
        assert all(original in visible(sessions, query) for original in originals[:2])
    source = SELECTION_SOURCE if names[0] == "Лёша" else f"{names[0]} верен, другую версию удали."
    selection = ingress(sessions, monkeypatch, source)
    requests = await run(
        sessions,
        selection,
        protocol,
        select_direction(names[0]),
        "Удаление ещё не выполнено. Подтвердите или отмените действие кнопкой.",
    )
    assert len(requests) == 1 and sum(wire_bytes(p) for p in requests) <= 16000
    schemas = (
        requests[0]["tools"]
        if protocol == "native"
        else json.loads(requests[0]["messages"][0]["content"])["tools"]
    )
    assert len(schemas) == 7
    with sessions() as session:
        assert session.get(Operation, selection).status == "done"
        invocation = session.scalar(select(Invocation).where(Invocation.operation_id == selection))
        assert invocation.model_result["data"]["state"] == "awaiting_approval"
        assert invocation.model_result["data"]["memory_changed"] is False
        row = session.scalar(select(Approval).where(Approval.operation_id == selection))
        assert row and row.payload["retain_entry_id"] == str(ids[0])
        assert row.payload["old_entry_id"] == str(ids[1]) and row.payload["schema"] == 2
        assert all(session.get(MemoryEntry, i) for i in ids)
        aid = row.id
    validate_memory_output(visible(sessions))
    await owner_callback(sessions, monkeypatch, aid, "y" if finish == "confirm" else "n")
    await owner_callback(sessions, monkeypatch, aid)
    with sessions() as session:
        assert session.get(MemoryEntry, ids[0]) and session.get(MemoryEntry, ids[2])
        assert bool(session.get(MemoryEntry, ids[1])) == (finish == "cancel")
        assert session.get(Approval, aid).status == (
            "executed" if finish == "confirm" else "cancelled"
        )
    if finish == "confirm":
        after = ingress(sessions, monkeypatch, "Кто теперь руководитель Сьерры?")
        await run(
            sessions,
            after,
            protocol,
            ("search_memory", {"query": "Сьерра"}),
            f"Руководитель команды Сьерра — {names[0]}.",
        )
        with sessions() as session:
            inv = session.scalar(select(Invocation).where(Invocation.operation_id == after))
            assert inv.result["data"]["shown_pairs"] == []
            assert len(inv.result["data"]["memory"]) == 2


@pytest.mark.parametrize(
    "final",
    [
        "Удаление ещё не выполнено. Подтвердите или отмените действие кнопкой.",
        "Я ничего не удалил. Карточка ждёт вашего подтверждения.",
        "Запись будет удалена после подтверждения.",
        "Старая запись не была удалена. Без подтверждения не удаляю.",
        "Я не стёр запись из памяти. Подтвердите действие кнопкой.",
        "Я ничего не стирал. Обе записи по-прежнему сохранены.",
        "Память не очищена. Старая запись не исчезла.",
        "Запись будет стёрта только после подтверждения.",
        "После подтверждения она больше не будет храниться в памяти.",
        "Память будет очищена после подтверждения.",
        "Нажмите «Подтвердить», чтобы старая запись была удалена.",
        "Чтобы старая запись была стёрта, подтвердите действие кнопкой.",
        "Подтвердите удаление, чтобы в памяти осталась только актуальная запись.",
        "Если нажмёте «Подтвердить», в памяти будет только актуальная версия.",
        "После подтверждения в памяти останется лишь актуальная запись.",
        "Запись будет полностью удалена после подтверждения.",
        "После подтверждения память будет окончательно очищена.",
        "Без подтверждения старая запись не была удалена и память не очищена.",
        "Удаление не выполнялось. Обе версии сохранены.",
        "В памяти пока не только актуальная запись. Обе версии сохранены.",
        "Я бы удалил старую запись только после подтверждения.",
        "Я ничего не удалил. Нажмите «Подтвердить», чтобы запись была удалена.",
        "Только актуальная запись останется в памяти после подтверждения.",
        "Лишь актуальная версия после подтверждения будет сохранена в памяти.",
        "Единственная актуальная запись будет храниться в памяти после подтверждения.",
    ],
)
def test_truthful_pending_output_allowed(final):
    from app.memory_output import validate_pending_output

    validate_pending_output(final)


@pytest.mark.parametrize("extra", ["search_after", "search_before", "save_after", "pdf_after"])
@pytest.mark.parametrize(
    "final,safe",
    [
        ("Я уже удалил Сашу.", False),
        ("Я ничего не удалил. Старая запись уже удалена.", False),
        ("Я ничего не удалил. Запись будет удалена после подтверждения.", True),
    ],
)
async def test_pending_deletion_guard_covers_same_native_response_tools(
    sessions, monkeypatch, tmp_path, extra, final, safe
):
    ids = await seed(sessions)
    await show(sessions, monkeypatch)
    with sessions() as session:
        from app.memory_resolution import snapshot

        before = {i: snapshot(session, session.get(MemoryEntry, i)) for i in ids}
    doc_id = None
    if extra == "pdf_after":
        from tests.test_memory_documents import upload

        manager, _, doc_id = await upload(sessions, tmp_path, ["Budget ORCHID is 9 dollars."])
        with sessions.begin() as session:
            job = claim(session, Job, 42, include_kinds={"pdf_index"})
        await manager.index(job, (job.id, job.lease_token))
        with sessions.begin() as session:
            session.get(Job, job.id).status = "done"
    op_id = ingress(
        sessions,
        monkeypatch,
        "Оставь Лёшу, другую версию удали. Проверь память и PDF. "
        "Запомни: бюджет Орхидеи 99 рублей.",
    )
    cfg = Settings(
        ai_api_key="test",
        ai_folder_id="folder",
        ai_attempts=1,
        pricing_path=Path("config/pricing.json"),
    )
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [1.0] + [0.0] * 255}]})
        requests.append(payload)
        if len(requests) == 1:
            assert len(payload["tools"]) == 7
            prepare = select_direction("Лёша")(payload)
            other = (
                ("search_document", {"query": "budget", "document_id": str(doc_id)})
                if extra == "pdf_after"
                else ("save_memory", {"text": "Бюджет Орхидеи 99 рублей."})
                if extra == "save_after"
                else ("search_memory", {"query": "Сьерра"})
            )
            calls = [other, prepare] if extra == "search_before" else [prepare, other]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call-{i}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                    for i, (name, args) in enumerate(calls)
                ],
            }
        else:
            content = final
            if extra == "pdf_after":
                raw = json.loads(payload["messages"][-1]["content"])
                # Escaped Unicode bypasses raw text matching; the rendered answer must be checked.
                content = json.dumps(
                    {
                        "has_evidence": True,
                        "answer": final,
                        "source_ids": [raw["sources"][0]["source_id"]],
                    }
                )
            message = {"role": "assistant", "content": content}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = YandexProvider(cfg, sessions, httpx.MockTransport(respond))
    await Runtime(sessions, provider, registry(sessions, provider, cfg)).run(op_id)
    assert len(requests) == 1
    with sessions() as session:
        op = session.get(Operation, op_id)
        card = session.scalar(select(Approval).where(Approval.operation_id == op_id))
        assert card.status == "pending" and card.action_kind == "memory_resolution_shown"
        assert card.payload["retain_entry_id"] == str(ids[0]) and card.payload[
            "old_entry_id"
        ] == str(ids[1])
        assert before == {i: snapshot(session, session.get(MemoryEntry, i)) for i in ids}
        assert op.input_spent <= 16000
        assert op.status == "done" and op.error_reason is None
        assert final not in visible(sessions, op_id)
        assert "Подтвердить" in json.dumps(
            [
                o.payload
                for o in session.scalars(select(Outbox).where(Outbox.operation_id == op_id))
            ],
            ensure_ascii=False,
        )


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("kind", ["shown", "legacy", "data_deletion"])
@pytest.mark.parametrize("safe", [True, False])
async def test_pending_deletion_guard_resumes_all_cached_destructive_kinds(
    sessions, monkeypatch, tmp_path, protocol, kind, safe
):
    from app.memory_resolution import snapshot
    from tests.test_memory_resolution import SOURCE

    if kind == "legacy":
        await fixture_pair(sessions)
        op_id = ingress(sessions, monkeypatch, SOURCE)
    else:
        await seed(sessions)
        await show(sessions, monkeypatch, protocol)
        op_id = (
            ingress(sessions, monkeypatch, "Очисти память", expected_kind="data_prepare")
            if kind == "data_deletion"
            else ingress(sessions, monkeypatch, "Оставь Лёшу, другую версию удали. Проверь память.")
        )
    with sessions() as session:
        before = {e.id: snapshot(session, e) for e in session.scalars(select(MemoryEntry))}
        op = session.get(Operation, op_id)
    cfg = Settings(
        ai_api_key="test",
        ai_folder_id="folder",
        ai_attempts=1,
        tool_protocol=protocol,
        file_directory=tmp_path,
    )
    provider = YandexProvider(
        cfg,
        sessions,
        httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"embedding": [1.0] + [0.0] * 255}]})
        ),
    )
    runtime = Runtime(sessions, provider, registry(sessions, provider, cfg), protocol)
    messages = runtime._messages(op)
    if kind == "shown":
        name, args = select_direction("Лёша")({"messages": messages})
    elif kind == "legacy":
        name, args = "prepare_memory_resolution", {"selector": "Петров"}
    else:
        name, args = "prepare_data_deletion", {"scope": "memory"}
    calls = [(name, args), ("search_memory", {"query": "руководитель"})]
    for index, (name, args) in enumerate(calls):
        call_id = f"cached-{index}"
        message = (
            {
                "role": "assistant",
                "content": json.dumps({"type": "tool", "name": name, "arguments": args}),
            }
            if protocol == "json"
            else {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }
        )
        with sessions.begin() as session:
            inv = Invocation(
                operation_id=op_id, ordinal=index, call_id=call_id, name=name, arguments=args
            )
            session.add(inv)
            session.add(History(operation_id=op_id, owner_id=42, message=message))
            session.flush()
        messages.append(message)
        messages.append(await runtime._invoke(op, inv, messages, None, legacy_pending=True))
    with sessions() as session:
        card = session.scalar(select(Approval).where(Approval.operation_id == op_id))
        assert card.status == "pending"
        aid, original_payload = card.id, card.payload
        preview = session.get(ApprovalPreview, aid)
        original_preview = preview.text if preview else None
        invs = session.scalars(
            select(Invocation).where(Invocation.operation_id == op_id).order_by(Invocation.ordinal)
        ).all()
        assert invs[-1].name == "search_memory" and all(i.result and i.model_result for i in invs)
    final = (
        "Я ничего не удалил. Удаление будет выполнено после подтверждения."
        if safe
        else "Я ничего не удалил. Память уже очищена."
    )
    requests = await run(sessions, op_id, protocol, final=final)
    assert len(requests) == (1 if kind == "data_deletion" else 0)
    with sessions() as session:
        card = session.get(Approval, aid)
        assert card.status == "pending" and card.payload == original_payload
        preview = session.get(ApprovalPreview, aid)
        assert (preview.text if preview else None) == original_preview
        assert before == {e.id: snapshot(session, e) for e in session.scalars(select(MemoryEntry))}
        op = session.get(Operation, op_id)
        assert op.status == ("done" if safe or kind != "data_deletion" else "error")
        assert op.input_spent <= 16000
        assert (final in visible(sessions, op_id)) == (safe and kind == "data_deletion")
        if not safe and kind == "data_deletion":
            assert op.error_reason == "invalid_final"


@pytest.mark.parametrize(
    "final",
    [
        "Я уже удалил Сашу.",
        "Я выполнил удаление.",
        "Удаление выполнено. Лёша остался.",
        "Я ничего не удалил. Старая запись уже удалена.",
        "Удаление не выполнено, но я убрал старую запись.",
        "Подтверждение не нужно: удаление возможно без подтверждения.",
        "Кнопка не нужна, всё выполнено.",
        "Я стёр устаревшую запись из памяти.",
        "Я ничего не стирал. Я стёр устаревшую запись из памяти.",
        "Я не стёр запись, но память очищена.",
        "Память очищена.",
        "Старой записи больше нет.",
        "Старая версия исчезла.",
        "Старая запись больше не существует.",
        "В памяти не осталось старой записи.",
        "Она уже не хранится в памяти.",
        "Я ничего не удалил. Она уже не хранится в памяти.",
        "В памяти теперь только актуальная запись.",
        "В памяти сейчас лишь правильная версия.",
        "Осталась единственная версия.",
        "В памяти теперь одна запись.",
        "Сохранена лишь актуальная версия.",
        "Я ничего не удалил. В памяти теперь только актуальная запись.",
        "В памяти теперь только актуальная запись. Я ничего не удалил.",
        "Я не стёр запись и в памяти теперь только актуальная запись.",
        "Память не очищена, но осталась единственная версия.",
        "Нажмите «Подтвердить», чтобы запись была удалена. Я уже удалил её.",
        "Я уже стёр запись. Нажмите «Подтвердить», чтобы память была очищена.",
        "После подтверждения я удалил старую запись.",
        "Только актуальная запись осталась в памяти.",
        "Лишь актуальная версия хранится в памяти.",
        "Только актуальная запись теперь в памяти. Я ничего не удалил.",
        "Только актуальная запись осталась в памяти и будет актуальна дальше.",
    ],
)
def test_affirmative_completion_never_hidden_by_negation(final):
    from app.memory_output import validate_pending_output
    from app.terminal import InvalidFinal

    with pytest.raises(InvalidFinal):
        validate_pending_output(final)


@pytest.mark.parametrize(
    "failure", ["clause", "extra_fact", "sentence_entity", "compound_value", "third"]
)
@pytest.mark.parametrize("partition", ["predicate", "value"])
async def test_person_role_unit_projection_rejects_incomplete_atomic_claim(
    sessions, monkeypatch, failure, partition
):
    from app.domain_memory import SearchArgs

    ids = await seed(
        sessions, names=("Элиан", "Зорвин", "Тарвин") if failure == "third" else ("Элиан", "Зорвин")
    )
    with sessions.begin() as session:
        for index, entry_id in enumerate(ids[:3] if failure == "third" else ids[:2]):
            fact = session.scalar(select(Fact).where(Fact.entry_id == entry_id))
            name = ("Элиан", "Зорвин", "Тарвин")[index]
            entity = Entity(owner_id=42, name=name.casefold())
            session.add(entity)
            session.flush()
            fact.entity_id, fact.predicate, fact.value = entity.id, "руководитель команды", "Сьерра"
            if partition == "value":
                fact.predicate, fact.value = "руководитель", "команды Сьерра"
        entry = session.get(MemoryEntry, ids[0])
        fact = session.scalar(select(Fact).where(Fact.entry_id == ids[0]))
        if failure == "clause":
            entry.original += ", люблю кофе"
        elif failure == "extra_fact":
            session.add(
                Fact(entry_id=entry.id, entity_id=fact.entity_id, predicate="дети", value="двое")
            )
        elif failure == "sentence_entity":
            session.get(Entity, fact.entity_id).name = "элиан любит кофе"
        elif failure == "compound_value":
            fact.value += " и любит кофе"
    ctx = durable_context(sessions, "Кто руководит Сьеррой?", "search_memory")
    handler = SearchMemory(sessions, Embeddings())
    args = SearchArgs(query="Сьерра")
    prepared = await handler.prepare(ctx, args)
    with sessions.begin() as session:
        result = handler.apply(session, ctx, args, prepared)
        assert result.data["shown_pairs"] == []

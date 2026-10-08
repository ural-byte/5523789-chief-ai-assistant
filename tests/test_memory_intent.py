import asyncio
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.bootstrap import install
from app.config import Settings
from app.domain_actions import Approval, CreateTask, Task
from app.domain_memory import Fact, MemoryEntry, SaveArgs, SaveMemory, SearchArgs, SearchMemory
from app.models import AICall, History, Invocation, Job, Operation, Outbox, Tombstone, Update, now
from app.privacy import rebuild_context
from app.providers import Generation, ProviderError, YandexProvider
from app.runtime import Runtime
from app.tools import Registry, ToolContext, registry
from tests.test_actions import ActionArgs, RelativeDate, callback, context
from tests.test_data_controls import prepare
from tests.test_memory_documents import Embeddings
from tests.test_storage import operation

SAVE_SOURCE = "Привет! Запомни: меня зовут Вова."
FINAL = "Буду помнить, что вас зовут Вова."


def input_operation(sessions, source, owner=42, update_id=7100):
    op_id = operation(sessions, owner)
    with sessions.begin() as session:
        session.add(Update(id=update_id, owner_id=owner, payload={"message": {"text": source}}))
        session.flush()
        session.get(Operation, op_id).update_id = update_id
        session.add(
            History(operation_id=op_id, owner_id=owner, message={"role": "user", "content": source})
        )
    return op_id


def production_provider(sessions, protocol, transport):
    config = Settings(
        _env_file=None,
        ai_api_key="dummy",
        ai_folder_id="folder",
        tool_protocol=protocol,
        ai_attempts=1,
        allowed_telegram_user_id=42,
    )
    provider = YandexProvider(config, sessions, httpx.MockTransport(transport))
    install(sessions, provider, config)
    return provider


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_llm_selected_save_actual_result_final_and_replay(sessions, protocol):
    # One scenario per protocol: the mocked gateway does not test phrase interpretation.
    source = SAVE_SOURCE
    op_id = input_operation(sessions, source)
    generations = []
    args = {
        "text": "меня зовут Вова",
        "facts": [{"entity": "пользователь", "predicate": "имя", "value": "Вова"}],
    }

    def transport(request):
        payload = json.loads(request.content)
        if "embeddings" in str(request.url):
            return httpx.Response(
                200,
                json={"data": [{"embedding": [1.0] + [0.0] * 255}], "usage": {"prompt_tokens": 10}},
            )
        generations.append(payload)
        if len(generations) == 1:
            if protocol == "native":
                tools = payload["tools"]
                message = {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "save",
                            "type": "function",
                            "function": {"name": "save_memory", "arguments": json.dumps(args)},
                        }
                    ],
                }
            else:
                instruction = json.loads(payload["messages"][0]["content"])
                tools = instruction["tools"]
                message = {
                    "content": json.dumps(
                        {"type": "tool", "name": "save_memory", "arguments": args}
                    )
                }
            assert {s["function"]["name"] for s in tools} == Registry.allowed_names
            with sessions() as session:
                op = session.get(Operation, op_id)
                runtime = Runtime(sessions, provider, registry, protocol)
                assert tools == runtime._schemas(session, op)
                messages = runtime._messages(op)
            assert (
                provider.estimate_request_budget(messages, tools)
                + (provider.estimate_request_budget(messages, []))
                <= 16000
            )
        else:
            model_messages = payload["messages"]
            raw = next(
                m["content"]
                for m in reversed(model_messages)
                if m["role"] == ("tool" if protocol == "native" else "user")
            )
            result = json.loads(raw if protocol == "native" else raw.split("(данные): ", 1)[1])
            assert result["status"] == "ok" and result["data"]["entry_id"]
            assert result["presentation"] == "model"
            with sessions() as session:
                assert not session.scalar(select(Outbox).where(Outbox.operation_id == op_id))
            message = {
                "content": FINAL
                if protocol == "native"
                else json.dumps({"type": "final", "text": FINAL})
            }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    provider = production_provider(sessions, protocol, transport)
    await Runtime(sessions, provider, registry, protocol).run(op_id)
    await Runtime(sessions, provider, registry, protocol).run(op_id)
    with sessions() as session:
        assert len(generations) == 2, {
            "input_spent": session.get(Operation, op_id).input_spent,
            "calls": [
                (c.operation_type, c.status, c.error_code) for c in session.scalars(select(AICall))
            ],
            "invocations": [
                (i.name, bool(i.result), bool(i.model_result))
                for i in session.scalars(select(Invocation))
            ],
        }
    with sessions() as session:
        entry = session.scalar(select(MemoryEntry))
        assert entry.source_text == source and entry.owner_id == 42
        assert session.get(Operation, op_id).status == "done"
        assert len(session.scalars(select(Fact)).all()) == 1
        outputs = session.scalars(select(Outbox).where(Outbox.operation_id == op_id)).all()
        assert [o.payload["text"] for o in outputs] == [FINAL]
        assert len(session.scalars(select(AICall)).all()) == 3
        assert all(
            c.cost is not None and c.latency_ms is not None for c in session.scalars(select(AICall))
        )
        assert (
            session.scalars(
                select(History).where(History.operation_id == op_id).order_by(History.id)
            )
            .all()[-1]
            .message["content"]
            == FINAL
        )
    retrieval = await SearchMemory(sessions, provider).prepare(
        context(sessions, "Как меня зовут?"), SearchArgs(query="имя", entity="пользователь")
    )
    assert retrieval["facts"][0]["value"] == "Вова"


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("failure", ["embedding", "final", "blank", "schema"])
async def test_save_failures_never_deliver_early_success(sessions, protocol, failure):
    op_id = input_operation(sessions, SAVE_SOURCE)
    generations = 0

    def transport(request):
        nonlocal generations
        if "embeddings" in str(request.url):
            return (
                httpx.Response(401)
                if failure == "embedding"
                else httpx.Response(
                    200,
                    json={
                        "data": [{"embedding": [1.0] + [0.0] * 255}],
                        "usage": {"prompt_tokens": 10},
                    },
                )
            )
        generations += 1
        if generations == 1:
            args = {"text": "имя Вова", **({"unexpected": True} if failure == "schema" else {})}
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "save",
                        "type": "function",
                        "function": {"name": "save_memory", "arguments": json.dumps(args)},
                    }
                ],
            }
            if protocol == "json":
                message = {
                    "content": json.dumps(
                        {"type": "tool", "name": "save_memory", "arguments": args}
                    )
                }
        elif failure == "final":
            return httpx.Response(401)
        else:
            text = "" if failure == "blank" else "Не удалось выполнить сохранение."
            message = {
                "content": text
                if protocol == "native"
                else json.dumps({"type": "final", "text": text})
            }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5},
            },
        )

    provider = production_provider(sessions, protocol, transport)
    await Runtime(sessions, provider, registry, protocol).run(op_id)
    with sessions() as session:
        assert bool(session.scalar(select(MemoryEntry))) == (failure in {"final", "blank"})
        assert session.get(Operation, op_id).status == ("done" if failure == "schema" else "error")
        outputs = session.scalars(select(Outbox).where(Outbox.operation_id == op_id)).all()
        assert len(outputs) == 1 and "Не удалось" in outputs[0].payload["text"]
        assert "Сохранил" not in outputs[0].payload["text"]
        assert session.scalar(select(AICall)) is not None
        if failure == "schema":
            assert session.scalar(select(Invocation)).result["status"] == "error"


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_legacy_canonical_save_recovery_uses_model_final(sessions, protocol):
    ctx = context(sessions, SAVE_SOURCE)
    handler = SaveMemory(sessions, Embeddings())
    args = SaveArgs(text="имя Вова")
    prepared = await handler.prepare(ctx, args)
    with sessions.begin() as session:
        result = handler.apply(session, ctx, args, prepared).model_dump()
        result["presentation"] = "canonical"
        inv = session.get(Invocation, uuid.UUID(ctx.idempotency_key))
        inv.name, inv.arguments, inv.result = "save_memory", args.model_dump(), result
        session.get(Operation, ctx.operation_id).tool_steps = 5

    class FinalProvider(Embeddings):
        def estimate_request_budget(self, messages, tools):
            return 100

        async def generate(self, operation_id, messages, tools, lease=None):
            assert not tools and "entry_id" in str(messages)
            return Generation(FINAL, [], {}, {}, "test")

    local_registry = Registry()
    local_registry.register("save_memory", handler)
    await Runtime(sessions, FinalProvider(), local_registry, protocol).run(ctx.operation_id)
    with sessions() as session:
        outputs = session.scalars(select(Outbox)).all()
        assert [o.payload["text"] for o in outputs] == [FINAL]
        assert len(session.scalars(select(MemoryEntry)).all()) == 1


@pytest.mark.parametrize("job_status", ["pending", "backoff", "running"])
async def test_unknown_pre_snapshot_producer_fenced_new_and_foreign_survive(sessions, job_status):
    old = input_operation(sessions, "Фиолетовый маркер 8a61", update_id=7200)
    with sessions.begin() as session:
        job = Job(
            key="captured",
            kind="agent",
            operation_id=old,
            payload={},
            status="running" if job_status == "running" else "pending",
            available_at=now() + timedelta(hours=1),
            attempts=int(job_status == "backoff"),
        )
        session.add(job)
    aid, _ = await prepare(sessions, "memory")
    middle = context(sessions, "Добавь это в мои заметки")
    foreign = context(sessions, "Добавь это в мои заметки", owner=99)
    with sessions() as session:
        original = session.get(Approval, aid).payload.copy()
        assert str(old) in original["operation_ids"] and str(old) not in original["producer_ids"]
    await callback(sessions, aid)
    await callback(sessions, aid)
    await callback(sessions, aid, choice="n")
    after = context(sessions, "Добавь это в мои заметки")
    with sessions.begin() as session:
        rebuild_context(session, old)
        rebuild_context(session, middle.operation_id)
        oldop = session.get(Operation, old)
        inv = Invocation(operation_id=old, name="save_memory", call_id="late", arguments={})
        session.add(inv)
        session.flush()
        oldctx = ToolContext(42, 42, old, oldop.reference_at, oldop.timezone, str(inv.id), {})
        assert session.get(Approval, aid).payload == original
        assert not session.get(Tombstone, f"operation:{old}")
        assert session.get(Job, job.id).status != "cancelled"
    provider = Embeddings()
    save = SaveMemory(sessions, provider)
    args = SaveArgs(
        text="private late value",
        facts=[{"entity": "x", "predicate": "p", "value": "private late value"}],
    )
    with pytest.raises(ProviderError, match="memory_write_revoked"):
        await save.prepare(oldctx, args)
    assert provider.calls == 0
    prepared = await provider.embed(old, args.text)
    with pytest.raises(ProviderError, match="memory_write_revoked"), sessions.begin() as session:
        save.apply(session, oldctx, args, prepared)
    for ctx in [middle, after, foreign]:
        with sessions.begin() as session:
            rebuild_context(session, ctx.operation_id)
        ready = await save.prepare(ctx, SaveArgs(text=str(ctx.operation_id)))
        with sessions.begin() as session:
            save.apply(session, ctx, SaveArgs(text=str(ctx.operation_id)), ready)
    # An unrelated task still runs from the captured operation after the context rebuild.
    with sessions.begin() as session:
        result = CreateTask().apply(
            session,
            oldctx,
            ActionArgs(
                text="task remains",
                date=RelativeDate(
                    kind="relative", source_phrase="через два часа", amount=2, unit="hours"
                ),
            ),
            (now() + timedelta(hours=2), None),
        )
        assert session.get(Task, uuid.UUID(result.data["task_id"]))
        assert len(session.scalars(select(MemoryEntry)).all()) == 3
        assert not session.scalar(select(Fact))


@pytest.mark.parametrize("choice", ["cancel", "expired", "foreign", "documents"])
async def test_unconfirmed_and_documents_only_do_not_fence_memory(sessions, choice):
    ctx = context(sessions, "Добавь в заметки: значение")
    aid, _ = await prepare(sessions, "documents" if choice == "documents" else "memory")
    if choice == "expired":
        with sessions.begin() as session:
            session.get(Approval, aid).expires_at = now() - timedelta(seconds=1)
    await callback(
        sessions,
        aid,
        choice="n" if choice == "cancel" else "y",
        owner=99 if choice == "foreign" else 42,
    )
    with sessions.begin() as session:
        if choice == "documents":
            rebuild_context(session, ctx.operation_id)
        assert not session.get(Tombstone, f"memory-write:{ctx.operation_id}")
    handler = SaveMemory(sessions, Embeddings())
    args = SaveArgs(text="значение")
    ready = await handler.prepare(ctx, args)
    with sessions.begin() as session:
        assert handler.apply(session, ctx, args, ready).status == "ok"


async def test_embedding_started_before_confirm_cannot_apply_after_rebuild(sessions):
    ctx = context(sessions, "Добавь в заметки 17fa")
    started, release = asyncio.Event(), asyncio.Event()

    class StalledEmbedding(Embeddings):
        async def embed(self, *args, **kwargs):
            started.set()
            await release.wait()
            return await super().embed(*args, **kwargs)

    provider = StalledEmbedding()
    handler = SaveMemory(sessions, provider)
    args = SaveArgs(text="retired data")
    pending = asyncio.create_task(handler.prepare(ctx, args))
    await started.wait()
    aid, _ = await prepare(sessions, "memory")
    await callback(sessions, aid)
    with sessions.begin() as session:
        rebuild_context(session, ctx.operation_id)
    release.set()
    prepared = await pending
    assert provider.calls == 1
    with pytest.raises(ProviderError, match="memory_write_revoked"), sessions.begin() as session:
        handler.apply(session, ctx, args, prepared)
    with sessions() as session:
        assert not session.scalar(select(MemoryEntry))


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_late_generation_save_arguments_never_persist(sessions, protocol):
    op_id = input_operation(sessions, "Маркер 814a")
    started, release = asyncio.Event(), asyncio.Event()
    secret = "retired private arguments 814a"

    async def transport(request):
        payload = json.loads(request.content)
        assert str(request.url).endswith("/chat/completions")
        tools = (
            payload.get("tools", [])
            if protocol == "native"
            else json.loads(payload["messages"][0]["content"])["tools"]
        )
        assert any(t["function"]["name"] == "save_memory" for t in tools)
        started.set()
        await release.wait()
        message = {
            "content": None,
            "tool_calls": [
                {
                    "id": "late",
                    "type": "function",
                    "function": {"name": "save_memory", "arguments": json.dumps({"text": secret})},
                }
            ],
        }
        if protocol == "json":
            message = {
                "content": json.dumps(
                    {"type": "tool", "name": "save_memory", "arguments": {"text": secret}}
                )
            }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    provider = production_provider(sessions, protocol, transport)
    runtime = Runtime(sessions, provider, registry, protocol)
    pending = asyncio.create_task(runtime.run(op_id))
    await started.wait()
    aid, _ = await prepare(sessions, "memory")
    await callback(sessions, aid)
    release.set()
    await pending
    with sessions() as session:
        assert session.get(Operation, op_id).status == "error"
        calls = session.scalars(select(AICall).where(AICall.operation_id == op_id)).all()
        assert len(calls) == 1 and calls[0].input_tokens == 100 and calls[0].output_tokens == 20
        assert calls[0].cost is not None and calls[0].latency_ms is not None
        assert not session.scalar(select(Invocation).where(Invocation.operation_id == op_id))
        assert not session.scalar(select(MemoryEntry))
        for model, field in [(History, "message"), (Invocation, "arguments"), (Outbox, "payload")]:
            assert all(
                secret not in str(getattr(row, field)) for row in session.scalars(select(model))
            )
        assert all(
            s["function"]["name"] != "save_memory"
            for s in runtime._schemas(session, session.get(Operation, op_id))
        )


async def test_two_selected_save_invocations_share_one_entry_and_foreign_context_rejected(sessions):
    from dataclasses import replace

    ctx = context(sessions, SAVE_SOURCE)
    handler, args = SaveMemory(sessions, Embeddings()), SaveArgs(text="меня зовут Вова")
    ready = await handler.prepare(ctx, args)
    with sessions.begin() as session:
        first = handler.apply(session, ctx, args, ready)
        new = Invocation(
            operation_id=ctx.operation_id,
            name="save_memory",
            call_id="second",
            arguments=args.model_dump(),
        )
        session.add(new)
        session.flush()
        second_ctx = replace(ctx, idempotency_key=str(new.id))
    ready = await handler.prepare(second_ctx, args)
    with sessions.begin() as session:
        second = handler.apply(session, second_ctx, args, ready)
        assert first.data == second.data
        assert len(session.scalars(select(MemoryEntry)).all()) == 1
    from app.queue import LeaseLost

    with pytest.raises(LeaseLost, match="foreign memory producer"):
        await handler.prepare(replace(ctx, owner_id=99), args)


async def test_selective_copy_with_live_task_cannot_start_new_memory_write(sessions):
    from tests.test_memory_resolution import durable_context, fixture_pair
    from tests.test_memory_resolution import prepare as selective_prepare

    ids, contexts, retrieval = await fixture_pair(sessions)
    with sessions.begin() as session:
        op = session.get(Operation, retrieval.operation_id)
        task_inv = Invocation(
            operation_id=op.id,
            name="create_task",
            call_id="live-task",
            arguments={},
            result={"status": "ok"},
        )
        session.add(task_inv)
        session.add(Job(key="live-copy", operation_id=op.id, kind="agent", payload={}))
    aid, _ = selective_prepare(sessions)
    assert aid
    with sessions.begin() as session:
        session.get(Operation, retrieval.operation_id).status = "pending"
    await callback(sessions, aid)
    with sessions.begin() as session:
        assert session.get(MemoryEntry, ids[1])
        assert session.get(Tombstone, f"memory-write:{retrieval.operation_id}")
        assert not session.get(Tombstone, f"memory-write:{contexts[1].operation_id}")
        assert session.scalar(select(Job).where(Job.key == "live-copy")).status == "pending"
        rebuild_context(session, retrieval.operation_id)
    new = durable_context(sessions, "unused", op_id=retrieval.operation_id)
    handler = SaveMemory(sessions, Embeddings())
    with pytest.raises(ProviderError, match="memory_write_revoked"):
        await handler.prepare(new, SaveArgs(text="old copied data"))


def test_save_projection_keeps_entry_identity_or_rejects_insufficient_budget():
    from app.providers import BudgetExceeded
    from app.runtime import project_result
    from app.tools import ToolResult

    identity = str(uuid.uuid4())
    result = ToolResult(
        status="ok", data={"entry_id": identity}, user_message="Сохранил запись " + "факт " * 200
    ).model_dump()
    projected = project_result(result, lambda value: not value["user_message"])
    assert projected["data"]["entry_id"] == identity and projected["status"] == "ok"
    with pytest.raises(BudgetExceeded):
        project_result(result, lambda value: not value["data"]["entry_id"])


async def test_actual_sized_json_save_with_extra_wire_envelope_reaches_model_final(sessions):
    from app.providers import wire_bytes

    op_id = input_operation(sessions, SAVE_SOURCE)
    with sessions.begin() as session:
        op = session.get(Operation, op_id)
        op.timezone = "Asia/Yekaterinburg"
    args = {
        "text": "Меня зовут Вова, это моё полное имя.",
        "facts": [{"entity": "пользователь", "predicate": "имя", "value": "Вова"}],
    }
    args_wire = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    assert len(args_wire.encode()) >= 152
    content = json.dumps(
        {"type": "tool", "name": "save_memory", "arguments": args},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert len(content.encode()) >= 193
    generations = []
    measurements = {}

    def legacy_size(messages, tools):
        payload = provider.generation_payload(messages, tools)
        instruction = payload["messages"][0]
        instruction["content"] = json.dumps(json.loads(instruction["content"]), ensure_ascii=False)
        return wire_bytes(payload)

    def transport(request):
        if str(request.url).endswith("/embeddings"):
            return httpx.Response(
                200,
                json={"data": [{"embedding": [1.0] + [0.0] * 255}], "usage": {"prompt_tokens": 14}},
            )
        payload = json.loads(request.content)
        generations.append(payload)
        if len(generations) == 1:
            runtime = Runtime(sessions, provider, registry, "json")
            with sessions() as session:
                op = session.get(Operation, op_id)
            messages = runtime._messages(op)
            with sessions() as session:
                tools = runtime._schemas(session, session.get(Operation, op_id))
            measurements["first_request"] = provider.estimate_request_budget(messages, tools)
            assert measurements["first_request"] == wire_bytes(payload)
            measurements["legacy_first_request"] = legacy_size(messages, registry.schemas())
            message = {"role": "assistant", "content": content, "reasoning_content": ""}
            # Provider-only metadata must not displace the factual result or final generation.
            target = 3277 + 2048
            while legacy_size([*messages, message], []) < target:
                message["reasoning_content"] += "x"
            measurements["base_final"] = provider.estimate_request_budget([*messages, message], [])
        else:
            raw = next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "user")
            result = json.loads(raw.split("(данные): ", 1)[1])
            with sessions() as session:
                entry = session.scalar(select(MemoryEntry))
                assert result["data"]["entry_id"] == str(entry.id)
            assert result["status"] == "ok" and result["user_message"]
            assert result["truncated"] is False
            tool_message = next(m for m in payload["messages"] if m["role"] == "assistant")
            assert set(tool_message) == {"role", "content"}
            assert json.loads(tool_message["content"]) == {
                "type": "tool",
                "name": "save_memory",
                "arguments": args,
            }
            measurements["post_tool_final"] = wire_bytes(payload)
            message = {"content": json.dumps({"type": "final", "text": FINAL})}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 180},
            },
        )

    provider = production_provider(sessions, "json", transport)
    await Runtime(sessions, provider, registry, "json").run(op_id)
    assert len(generations) == 2
    assert measurements["first_request"] < measurements["legacy_first_request"] - 600
    assert 16000 - (measurements["first_request"] + measurements["post_tool_final"]) >= 900
    with sessions() as session:
        op = session.get(Operation, op_id)
        assert op.status == "done"
        assert op.input_spent == measurements["first_request"] + measurements["post_tool_final"]
        assert len(session.scalars(select(MemoryEntry)).all()) == 1
        assert len(session.scalars(select(Fact)).all()) == 1
        assert [o.payload["text"] for o in session.scalars(select(Outbox))] == [FINAL]
        assert len(session.scalars(select(AICall)).all()) == 3


@pytest.mark.parametrize("status", ["executing", "executed"])
@pytest.mark.parametrize("scope", ["memory", "reset", "documents"])
async def test_legacy_approved_deletion_without_marker_keeps_captured_write_boundary(
    sessions, tmp_path, status, scope
):
    from app.privacy import memory_write_allowed
    from app.queue import LeaseLost
    from tests.test_data_controls import drain

    old = input_operation(sessions, "Сделай заметку на будущее: значение", update_id=7300)
    foreign = context(sessions, "Новая заметка", owner=99)
    aid, _ = await prepare(sessions, scope)
    middle = context(sessions, "Новая заметка")
    with sessions() as session:
        original = session.get(Approval, aid).payload.copy()
        assert str(old) in original["operation_ids"]
        assert (str(old) in original["producer_ids"]) == (scope == "reset")
    await callback(sessions, aid)
    if status == "executed":
        await drain(sessions, tmp_path)
    with sessions.begin() as session:
        # Simulate the durable state of the prior version, without changing the approved payload.
        marker = session.get(Tombstone, f"memory-write:{old}")
        if marker:
            session.delete(marker)
        session.flush()
        approval = session.get(Approval, aid)
        assert approval.status == status and approval.approved_at
        assert approval.payload == original
        op = session.get(Operation, old)
        assert memory_write_allowed(session, op) == (scope == "documents")
        if scope != "reset":
            rebuild_context(session, old)
        inv = Invocation(operation_id=old, name="create_task", call_id="legacy-late", arguments={})
        session.add(inv)
        session.flush()
        oldctx = ToolContext(42, 42, old, op.reference_at, op.timezone, str(inv.id), {})
    provider = Embeddings()
    save = SaveMemory(sessions, provider)
    args = SaveArgs(text="old source fact")
    if scope in {"memory", "reset"}:
        with pytest.raises((ProviderError, LeaseLost)):
            await save.prepare(oldctx, args)
        assert provider.calls == 0
        ready = await Embeddings().embed(old, args.text)
        with pytest.raises((ProviderError, LeaseLost)), sessions.begin() as session:
            save.apply(session, oldctx, args, ready)
    else:
        ready = await save.prepare(oldctx, args)
        with sessions.begin() as session:
            assert save.apply(session, oldctx, args, ready).status == "ok"
    after = context(sessions, "Новая заметка")
    for ctx in [middle, after, foreign]:
        with sessions.begin() as session:
            rebuild_context(session, ctx.operation_id)
        ready = await save.prepare(ctx, SaveArgs(text=str(ctx.operation_id)))
        with sessions.begin() as session:
            assert (
                save.apply(session, ctx, SaveArgs(text=str(ctx.operation_id)), ready).status == "ok"
            )
    with sessions.begin() as session:
        assert session.get(Approval, aid).payload == original
        assert len(session.scalars(select(MemoryEntry)).all()) == (4 if scope == "documents" else 3)
        local_registry = Registry()
        local_registry.register("save_memory", save)
        local_registry.register("create_task", CreateTask())
        schemas = Runtime(sessions, provider, local_registry)._schemas(
            session, session.get(Operation, old)
        )
        assert ("save_memory" in {s["function"]["name"] for s in schemas}) == (scope == "documents")
        assert "create_task" in {s["function"]["name"] for s in schemas}
        if scope == "memory":
            result = CreateTask().apply(
                session,
                oldctx,
                ActionArgs(
                    text="legacy task survives",
                    date=RelativeDate(
                        kind="relative", source_phrase="через два часа", amount=2, unit="hours"
                    ),
                ),
                (now() + timedelta(hours=2), None),
            )
            assert session.get(Task, uuid.UUID(result.data["task_id"]))


@pytest.mark.parametrize("corruption", ["hash", "owner"])
async def test_legacy_approved_snapshot_integrity_error_prevents_embedding(sessions, corruption):
    from app.data_controls import digest

    ctx = context(sessions, "Сделай заметку на будущее: значение")
    aid, _ = await prepare(sessions, "memory")
    control_ctx = context(sessions, "legacy approval metadata")
    with sessions.begin() as session:
        payload = session.get(Approval, aid).payload.copy()
        if corruption == "owner":
            payload["owner_id"] = 99
            payload["hash"] = digest({k: v for k, v in payload.items() if k != "hash"})
        else:
            payload["hash"] = "invalid"
        session.add(
            Approval(
                owner_id=42,
                chat_id=42,
                operation_id=control_ctx.operation_id,
                invocation_id=uuid.UUID(control_ctx.idempotency_key),
                action_kind="data_deletion",
                status="executed",
                approved_at=now(),
                executed_at=now(),
                expires_at=now() + timedelta(hours=24),
                payload=payload,
            )
        )
    provider = Embeddings()
    handler = SaveMemory(sessions, provider)
    with pytest.raises(ProviderError, match="invalid_deletion_snapshot"):
        await handler.prepare(ctx, SaveArgs(text="value"))
    assert provider.calls == 0
    with sessions() as session:
        assert not session.scalar(select(MemoryEntry))

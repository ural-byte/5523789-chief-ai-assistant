import asyncio
import itertools
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.domain_actions import Approval, Task
from app.domain_memory import Fact, MemoryEntry
from app.models import ApprovalAudit, Invocation, Operation, Update, now
from app.spike_atelier.api import create_app
from app.spike_atelier.config import SpikeSettings
from app.spike_atelier.receipts import SpikeReceipt
from app.spike_atelier.service import BindingRejected, SpikeService
from app.spike_atelier.transport import deliver_once


class EmbeddingsOnly:
    def __init__(self):
        self.calls = 0

    async def embed(self, *args):
        self.calls += 1
        return SimpleNamespace(vector=[1.0] + [0.0] * 255, model="test-embedding")

    async def generate(self, *args):
        raise AssertionError("adapter must never invoke conversational generation")


@pytest.fixture
def config():
    return SpikeSettings(
        database_url="postgresql+psycopg://spike:spike-test-only@localhost:55450/atelier_spike_test",
        owner_id=42,
        chat_id=42,
        mcp_secret="m" * 32,
        transport_secret="t" * 32,
        binding_secret="b" * 32,
        binding_verified=True,
        binding_evidence="local-test-fixture; not Atelier live proof",
    )


@pytest.fixture
def kit(sessions, config):
    return SpikeService(sessions, EmbeddingsOnly(), config)


_sequence = itertools.count(1)


def event(text, owner=42, chat=42):
    return {
        "update_id": next(_sequence),
        "message": {
            "from": {"id": owner},
            "chat": {"id": chat, "type": "private"},
            "date": int(now().timestamp()),
            "text": text,
        },
    }


def binding(kit, text):
    return kit.ingest(event(text))[1]


async def save(kit, value, entity="Сьерра"):
    return await kit.call(
        binding(kit, f"Запомни: руководитель команды {entity} — {value}"),
        "save_memory",
        {
            "text": f"руководитель команды {entity} — {value}",
            "facts": [{"entity": entity, "predicate": "руководитель", "value": value}],
        },
    )


async def drain(kit, success=True):
    messages = []

    def telegram(request):
        import json

        payload = json.loads(request.content)
        messages.append(payload)
        return httpx.Response(
            200,
            json={
                "ok": success,
                "result": {"message_id": len(messages), "chat": {"id": payload.get("chat_id")}},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(telegram),
        base_url="https://telegram/",
    ) as client:
        for _ in range(30):
            if not await deliver_once(kit, client):
                break
        else:
            raise AssertionError("outbox did not drain")
    return messages


async def conflict(kit, deliver=True):
    old = (await save(kit, "Саша"))["data"]["entry_id"]
    keep = (await save(kit, "Лёша"))["data"]["entry_id"]
    unrelated = (await save(kit, "Ирина", "Кедр"))["data"]["entry_id"]
    result = await kit.call(
        binding(kit, "Кто руководитель команды Сьерра?"),
        "search_memory",
        {
            "query": "руководитель команды Сьерра",
            "entity": "Сьерра",
        },
    )
    pair = next(p for p in result["data"]["shown_pairs"] if "Сьерра" in p["a"])
    args = {"conflict_ref": pair["conflict_ref"], "retain_ref": "a" if "Лёша" in pair["a"] else "b"}
    if deliver:
        messages = await drain(kit)
        assert any("Лёша" in m["text"] and "Саша" in m["text"] for m in messages)
    return args, tuple(UUID(i) for i in (old, keep, unrelated))


async def choose(kit, args):
    return await kit.call(binding(kit, "Лёша — правильный вариант"), "delete_memory", args)


def callback(kit, approval_id, choice="y", owner=42, chat=42):
    with kit.sessions() as session:
        receipt = session.scalar(
            select(SpikeReceipt).where(
                SpikeReceipt.approval_id == UUID(approval_id),
            )
        )
        message_id = receipt.message_id if receipt else -1
    return {
        "update_id": next(_sequence),
        "callback_query": {
            "id": f"callback-{next(_sequence)}",
            "from": {"id": owner},
            "data": f"a:{approval_id}:{choice}",
            "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}},
        },
    }


async def test_exact_pair_buttons_callback_and_fresh_retrieval(kit):
    args, (old, keep, unrelated) = await conflict(kit)
    before = kit.tools["save_memory"].provider.calls
    result = await choose(kit, args)
    data = result["data"]
    aid = data["approval_id"]
    assert data["state"] == "awaiting_approval" and data["choice_recorded"] is True
    assert data["keep_entry_id"] == str(keep) and data["delete_entry_id"] == str(old)
    with kit.sessions() as session:
        assert all(session.get(MemoryEntry, i) for i in (old, keep, unrelated))
        card = session.get(Approval, UUID(aid))
        assert card.payload["choice_recorded"] == {
            "keep_entry_id": str(keep),
            "delete_entry_id": str(old),
        }
    messages = await drain(kit)
    assert len(messages) == 1
    assert [b["callback_data"] for b in messages[0]["reply_markup"]["inline_keyboard"][0]] == [
        f"a:{aid}:y",
        f"a:{aid}:n",
    ]
    kit.ingest(callback(kit, aid))
    assert await kit.callbacks_once()
    kit.ingest(callback(kit, aid))
    assert await kit.callbacks_once()
    assert kit.tools["save_memory"].provider.calls == before
    with kit.sessions() as session:
        assert not session.get(MemoryEntry, old)
        assert session.get(MemoryEntry, keep) and session.get(MemoryEntry, unrelated)
        assert session.get(Approval, UUID(aid)).status == "executed"
        assert session.scalar(select(Fact.id).where(Fact.entry_id == old)) is None
        assert len(session.scalars(select(ApprovalAudit)).all()) == 2
    # No model history: a newly bound event triggers a genuinely fresh PostgreSQL query.
    result = await kit.call(
        binding(kit, "Кто теперь руководитель Сьерры?"),
        "search_memory",
        {
            "query": "Сьерра",
            "entity": "Сьерра",
        },
    )
    assert result["data"]["conflicts"] == []
    assert result["data"]["shown_pairs"] == []
    assert {f["value"] for f in result["data"]["facts"]} == {"Лёша"}


@pytest.mark.parametrize("text", ["да", "удалить", "a"])
async def test_recorded_choice_survives_plain_confirm_and_reversed_replay(kit, text):
    args, ids = await conflict(kit)
    chosen = await choose(kit, args)
    await drain(kit)
    # The agent supplies the already selected structured references; backend ignores prose.
    kit = SpikeService(kit.sessions, EmbeddingsOnly(), kit.config)
    again = await kit.call(
        binding(kit, text),
        "delete_memory",
        {
            **args,
            "retain_ref": "b" if args["retain_ref"] == "a" else "a",
        },
    )
    assert again["data"] == chosen["data"]
    assert "Уточните" not in again["user_message"]
    with kit.sessions() as session:
        assert len(session.scalars(select(Approval)).all()) == 1
        assert all(session.get(MemoryEntry, i) for i in ids)


async def test_missing_or_failed_conflict_ack_cannot_prepare_approval(kit):
    args, ids = await conflict(kit, deliver=False)
    for failed_send in (False, True):
        if failed_send:
            await drain(kit, success=False)
        result = await choose(kit, args)
        assert result["status"] == "needs_clarification"
        with kit.sessions() as session:
            assert session.scalar(select(Approval.id)) is None
            assert all(session.get(MemoryEntry, i) for i in ids)


async def test_approval_card_must_be_delivered_and_exact_message_callback(kit):
    args, ids = await conflict(kit)
    aid = (await choose(kit, args))["data"]["approval_id"]
    with pytest.raises(BindingRejected, match="approval_not_delivered"):
        kit.ingest(callback(kit, aid))
    await drain(kit)
    wrong = callback(kit, aid)
    wrong["callback_query"]["message"]["message_id"] += 100
    with pytest.raises(BindingRejected, match="approval_not_delivered"):
        kit.ingest(wrong)
    with kit.sessions() as session:
        assert all(session.get(MemoryEntry, i) for i in ids)


@pytest.mark.parametrize("case", ["cancel", "expired", "stale", "foreign_owner", "foreign_chat"])
async def test_callback_safety(kit, case):
    args, ids = await conflict(kit)
    aid = (await choose(kit, args))["data"]["approval_id"]
    await drain(kit)
    with kit.sessions.begin() as session:
        if case == "expired":
            session.get(Approval, UUID(aid)).expires_at = now() - timedelta(seconds=1)
        elif case == "stale":
            session.get(MemoryEntry, ids[0]).original += " (изменено)"
    payload = callback(
        kit,
        aid,
        "n" if case == "cancel" else "y",
        owner=99 if case == "foreign_owner" else 42,
        chat=99 if case == "foreign_chat" else 42,
    )
    if case.startswith("foreign"):
        with pytest.raises(BindingRejected, match="foreign_event"):
            kit.ingest(payload)
        expected = "pending"
    else:
        kit.ingest(payload)
        await kit.callbacks_once()
        expected = {"cancel": "cancelled", "expired": "expired", "stale": "stale"}[case]
    with kit.sessions() as session:
        assert session.get(Approval, UUID(aid)).status == expected
        assert all(session.get(MemoryEntry, i) for i in ids)


async def test_exact_event_replay_is_idempotent_and_mismatched_source_rejected(kit):
    source = event("Запомни: кофе")
    op, token = kit.ingest(source)
    assert kit.ingest(source) == (op, token)
    first = await kit.call(token, "save_memory", {"text": "кофе"})
    second = await kit.call(token, "save_memory", {"text": "кофе"})
    assert first == second
    assert kit.tools["save_memory"].provider.calls == 1
    with kit.sessions() as session:
        assert len(session.scalars(select(MemoryEntry)).all()) == 1
        assert len(session.scalars(select(Invocation)).all()) == 1
        assert session.get(Update, source["update_id"]).payload == source
    source["message"]["text"] = "подмена"
    with pytest.raises(BindingRejected, match="event_replay_mismatch"):
        kit.ingest(source)


async def test_concurrent_choices_share_single_immutable_pair(kit):
    args, ids = await conflict(kit)
    tokens = [binding(kit, "Лёша — правильный вариант") for _ in range(2)]

    def invoke(token):
        return asyncio.run(kit.call(token, "delete_memory", args))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, tokens))
    assert results[0]["data"] == results[1]["data"]
    with kit.sessions() as session:
        assert len(session.scalars(select(Approval)).all()) == 1
        assert all(session.get(MemoryEntry, i) for i in ids)


async def test_paged_reads_owner_scope_and_backend_task_deadline(kit):
    await save(kit, "Саша")
    await save(kit, "Лёша")
    token = binding(kit, "Покажи память")
    first = await kit.call(token, "list_memory", {"limit": 1})
    second = await kit.call(
        token,
        "list_memory",
        {
            "limit": 1,
            "after": first["data"]["next_cursor"],
        },
    )
    assert first["data"]["items"][0]["entry_id"] != second["data"]["items"][0]["entry_id"]
    assert second["data"]["next_cursor"] is None
    other = SpikeService(
        kit.sessions,
        EmbeddingsOnly(),
        kit.config.model_copy(
            update={
                "owner_id": 99,
                "chat_id": 99,
            }
        ),
    )
    other_token = other.ingest(event("Покажи память", owner=99, chat=99))[1]
    assert (await other.call(other_token, "list_memory", {}))["data"]["items"] == []
    with pytest.raises(ValueError, match="invalid_cursor"):
        await other.call(other_token, "list_memory", {"after": first["data"]["next_cursor"]})
    task_token = binding(kit, "Напомни через 2 часа проверить отчёт")
    created = await kit.call(
        task_token,
        "create_task",
        {
            "text": "проверить отчёт",
            "date": {
                "kind": "relative",
                "source_phrase": "через 2 часа",
                "amount": 2,
                "unit": "hours",
            },
        },
    )
    tasks = await kit.call(task_token, "list_tasks", {})
    assert tasks["data"]["items"][0]["task_id"] == created["data"]["task_id"]
    assert (await other.call(other_token, "list_tasks", {}))["data"]["items"] == []
    with kit.sessions() as session:
        task = session.get(Task, UUID(created["data"]["task_id"]))
        assert task.deadline == task.reference_at + timedelta(hours=2)
        assert task.source_timezone == "Asia/Yekaterinburg"


@pytest.mark.parametrize(
    "authority",
    [
        "owner_id",
        "chat_id",
        "source_update",
        "call_id",
        "idempotency_key",
        "approved",
        "delivery_ack",
    ],
)
async def test_model_authority_fields_rejected_before_embedding(kit, authority):
    with pytest.raises(ValidationError):
        await kit.call(
            binding(kit, "Запомни факт"),
            "save_memory",
            {
                "text": "факт",
                authority: "spoofed",
            },
        )
    assert kit.tools["save_memory"].provider.calls == 0


async def test_gate_binding_and_transport_credentials_fail_closed(kit):
    app = TestClient(create_app(kit))
    headers = {"Authorization": "Bearer " + "m" * 32}
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "save_memory", "arguments": {"text": "spoofed"}},
    }
    assert app.post("/mcp", json=call, headers=headers).status_code == 403
    assert (
        app.post(
            "/transport/updates",
            json=event("spoofed"),
            headers={
                "X-Telegram-Bot-Api-Secret-Token": "m" * 32,
            },
        ).status_code
        == 401
    )
    token = binding(kit, "источник")
    assert (
        app.post(
            "/mcp",
            json=call,
            headers={
                **headers,
                "X-Spike-Event-Binding": token[:-1] + ("0" if token[-1] != "0" else "1"),
            },
        ).status_code
        == 403
    )
    kit.config = kit.config.model_copy(update={"binding_verified": False})
    assert app.post("/mcp", json=call, headers=headers).status_code == 503
    assert kit.tools["save_memory"].provider.calls == 0


async def test_mcp_protocol_schemas_real_call_and_redacted_errors(kit):
    app = TestClient(create_app(kit))
    headers = {"Authorization": "Bearer " + "m" * 32}
    result = app.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {},
        },
    )
    assert result.json()["result"]["capabilities"] == {"tools": {}}
    listed = app.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
        },
    ).json()["result"]["tools"]
    assert {tool["name"] for tool in listed} == {
        "save_memory",
        "search_memory",
        "list_memory",
        "delete_memory",
        "create_task",
        "list_tasks",
    }
    task_schema = next(t["inputSchema"] for t in listed if t["name"] == "create_task")
    assert "complete_request" not in task_schema["properties"]
    source = event("Запомни: кофе")
    ingested = app.post(
        "/transport/updates",
        json=source,
        headers={
            "X-Telegram-Bot-Api-Secret-Token": "t" * 32,
        },
    ).json()
    call_headers = {**headers, "X-Spike-Event-Binding": ingested["event_binding"]}
    response = app.post(
        "/mcp",
        headers=call_headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "save_memory", "arguments": {"text": "кофе"}},
        },
    )
    assert response.json()["result"]["isError"] is False
    for tool in ("confirm", "execute_delete", "prepare_meeting"):
        error = app.post(
            "/mcp",
            headers=call_headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {"token": "sensitive"}},
            },
        )
        assert error.json()["error"]["code"] == -32602
        assert "sensitive" not in error.text and "кофе" not in error.text


def test_isolated_settings_reject_production_database_and_merged_credentials(config):
    for changes in (
        {"database_url": "postgresql://localhost/assistant"},
        {"transport_secret": "m" * 32},
        {"binding_evidence": ""},
    ):
        with pytest.raises(ValidationError):
            SpikeSettings.model_validate({**config.model_dump(), **changes})


@pytest.mark.parametrize(
    "receipt",
    [
        {"ok": False, "result": {"message_id": 1, "chat": {"id": 42}}},
        {"ok": True, "result": {"chat": {"id": 42}}},
        {"ok": True, "result": {"message_id": 1, "chat": {"id": 99}}},
    ],
)
async def test_invalid_telegram_receipt_never_grants_shown_authority(kit, receipt):
    args, ids = await conflict(kit, deliver=False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=receipt)),
        base_url="https://telegram/",
    ) as bot:
        assert await deliver_once(kit, bot)
    result = await choose(kit, args)
    assert result["status"] == "needs_clarification"
    with kit.sessions() as session:
        assert session.scalar(select(SpikeReceipt)) is None
        assert all(session.get(MemoryEntry, i) for i in ids)


@pytest.mark.parametrize("changed", ["payload", "preview"])
async def test_approval_payload_and_preview_are_immutable(kit, changed):
    from app.models import ApprovalPreview

    args, _ = await conflict(kit)
    aid = UUID((await choose(kit, args))["data"]["approval_id"])
    with pytest.raises(ValueError, match="immutable"):
        with kit.sessions.begin() as session:
            if changed == "payload":
                row = session.get(Approval, aid)
                row.payload = {**row.payload, "owner_id": 99}
            else:
                session.get(ApprovalPreview, aid).text = "подмена"


async def test_expired_bound_event_and_retired_memory_producer_cannot_write(kit):
    from app.privacy import ContextChanged
    from app.terminal import ExecutionExpired

    source = event("Запомни: факт")
    op, token = kit.ingest(source)
    with kit.sessions.begin() as session:
        session.get(Operation, op).deadline_at = now() - timedelta(seconds=1)
    with pytest.raises(ExecutionExpired):
        await kit.call(token, "save_memory", {"text": "факт"})
    assert kit.tools["save_memory"].provider.calls == 0
    args, ids = await conflict(kit)
    with kit.sessions() as session:
        producer = session.get(Invocation, session.get(MemoryEntry, ids[0]).invocation_id)
        old_token = kit._token(producer.operation_id)
    aid = (await choose(kit, args))["data"]["approval_id"]
    await drain(kit)
    kit.ingest(callback(kit, aid))
    await kit.callbacks_once()
    with pytest.raises((BindingRejected, ContextChanged)):
        await kit.call(old_token, "save_memory", {"text": "восстановить удалённую запись"})
    with kit.sessions() as session:
        assert not session.get(MemoryEntry, ids[0])


async def test_request_notification_cannot_mutate_and_trace_contains_no_source(kit, caplog):
    import logging

    app = TestClient(create_app(kit))
    token = binding(kit, "private-source-phrase")
    headers = {"Authorization": "Bearer " + "m" * 32, "X-Spike-Event-Binding": token}
    result = app.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "save_memory", "arguments": {"text": "private-source-phrase"}},
        },
    )
    assert result.json()["error"]["code"] == -32602
    assert kit.tools["save_memory"].provider.calls == 0
    with caplog.at_level(logging.INFO, logger="spike.tools"):
        await kit.call(token, "save_memory", {"text": "private-source-phrase"})
    assert "tool_applied" in caplog.text and "latency_ms=" in caplog.text
    assert "private-source-phrase" not in caplog.text and token not in caplog.text


async def test_plaintext_actual_callback_data_does_not_execute(kit):
    args, ids = await conflict(kit)
    aid = (await choose(kit, args))["data"]["approval_id"]
    await drain(kit)
    for text in ("да", "удалить", "a", f"a:{aid}:y"):
        op, token = kit.ingest(event(text))
        assert token
        assert not await kit.callbacks_once()
        with kit.sessions() as session:
            assert session.get(Operation, op).scenario == "spike_event"
            assert session.get(Approval, UUID(aid)).status == "pending"
            assert all(session.get(MemoryEntry, i) for i in ids)


async def test_browser_origin_rejected_before_tool_and_get_has_no_sse(kit):
    app = TestClient(create_app(kit))
    token = binding(kit, "Запомни факт")
    headers = {
        "Authorization": "Bearer " + "m" * 32,
        "X-Spike-Event-Binding": token,
        "Origin": "https://untrusted.example",
    }
    assert (
        app.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "save_memory", "arguments": {"text": "факт"}},
            },
        ).status_code
        == 403
    )
    assert kit.tools["save_memory"].provider.calls == 0
    assert app.get("/mcp").status_code == 405


async def test_callback_failure_releases_lease_for_retry_without_deletion(kit, monkeypatch):
    from app.models import Job
    from app.spike_atelier import service as service_module

    args, ids = await conflict(kit)
    aid = (await choose(kit, args))["data"]["approval_id"]
    await drain(kit)
    op, _ = kit.ingest(callback(kit, aid))
    original = service_module.handle_callback

    async def fail(*args):
        raise RuntimeError("injected transport failure")

    monkeypatch.setattr(service_module, "handle_callback", fail)
    with pytest.raises(RuntimeError):
        await kit.callbacks_once()
    with kit.sessions.begin() as session:
        job = session.scalar(select(Job).where(Job.operation_id == op))
        assert job.status == "pending" and job.lease_token is None
        job.available_at = now() - timedelta(seconds=1)
        assert all(session.get(MemoryEntry, i) for i in ids)
    monkeypatch.setattr(service_module, "handle_callback", original)
    assert await kit.callbacks_once()
    with kit.sessions() as session:
        assert session.get(Approval, UUID(aid)).status == "executed"


def test_bootstrap_cannot_import_production_credentials(config, monkeypatch):
    from app.spike_atelier import __main__ as startup

    captured = {}
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "production-token-sentinel")
    monkeypatch.setenv("SERVICE_TOKEN", "production-service-sentinel")
    monkeypatch.setenv("AI_API_KEY", "production-ai-sentinel")
    monkeypatch.setattr(startup, "SpikeSettings", lambda: config)
    monkeypatch.setattr(startup, "make_sessions", lambda url: None)

    def provider(provider_config, sessions):
        captured["provider"] = provider_config
        return EmbeddingsOnly()

    monkeypatch.setattr(startup, "YandexProvider", provider)
    assert startup.application()
    provider_config = captured["provider"]
    assert provider_config.telegram_bot_token.get_secret_value() == ""
    assert provider_config.service_token.get_secret_value() == ""
    assert provider_config.ai_api_key.get_secret_value() == ""
    assert provider_config.ai_folder_id == ""


async def test_spike_scheduler_uses_existing_idempotent_reminder_engine(kit):
    from app.domain_actions import scheduler_once
    from app.models import Outbox

    token = binding(kit, "Напомни через 2 часа проверить отчёт")
    result = await kit.call(
        token,
        "create_task",
        {
            "text": "проверить отчёт",
            "date": {
                "kind": "relative",
                "source_phrase": "через 2 часа",
                "amount": 2,
                "unit": "hours",
            },
        },
    )
    task_id = UUID(result["data"]["task_id"])
    with kit.sessions() as session:
        deadline = session.get(Task, task_id).deadline
    scheduler_once(kit.sessions, kit.config.owner_id, deadline)
    scheduler_once(kit.sessions, kit.config.owner_id, deadline)
    with kit.sessions() as session:
        rows = session.scalars(
            select(Outbox).where(
                Outbox.key == f"task:{task_id}:reminder",
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].payload["text"] == "Напоминание: проверить отчёт"
        assert session.get(Task, task_id).status == "notified"
    assert (await kit.call(binding(kit, "Покажи активные поручения"), "list_tasks", {}))["data"][
        "items"
    ] == []


@pytest.mark.parametrize("malformed", ["wrong shape", None, []])
@pytest.mark.parametrize(
    "path",
    [
        "callback_message",
        "callback_from",
        "callback_chat",
        "message_from",
        "message_chat",
    ],
)
async def test_malformed_transport_nested_shapes_rejected_without_side_effects(
    kit, path, malformed
):
    from app.models import Job, Outbox, UserState

    payload = event("Запомни: факт")
    if path.startswith("callback"):
        message = payload.pop("message")
        payload["callback_query"] = {
            "id": "malformed-callback",
            "from": {"id": 42},
            "data": "a:00000000-0000-0000-0000-000000000001:y",
            "message": {"message_id": 1, "chat": message["chat"]},
        }
        query = payload["callback_query"]
        if path == "callback_message":
            query["message"] = malformed
        elif path == "callback_from":
            query["from"] = malformed
        else:
            query["message"]["chat"] = malformed
    else:
        payload["message"]["from" if path == "message_from" else "chat"] = malformed
    response = TestClient(create_app(kit)).post(
        "/transport/updates",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "t" * 32},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "event_rejected"}
    assert kit.tools["save_memory"].provider.calls == 0
    with kit.sessions() as session:
        for model in (Update, Operation, Job, Outbox, UserState, Approval, MemoryEntry):
            assert session.scalar(select(model)) is None

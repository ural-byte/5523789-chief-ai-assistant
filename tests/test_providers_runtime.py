import json
import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.config import Settings
from app.models import AICall, History, Invocation, Operation, Outbox
from app.pricing import Usage
from app.providers import (
    BudgetExceeded,
    Generation,
    ProviderError,
    ToolCall,
    YandexProvider,
    usage_totals,
)
from app.runtime import Runtime
from app.tools import Registry, ToolResult
from tests.test_storage import operation


def config(protocol="native", attempts=1):
    return Settings(
        ai_api_key="test",
        ai_folder_id="folder",
        tool_protocol=protocol,
        ai_attempts=attempts,
        pricing_path=Path("config/pricing.json"),
    )


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str


class SearchTool:
    description = "Поиск по PDF"
    arguments = Arguments
    applies = 0

    async def prepare(self, ctx, arguments):
        return arguments.query

    def apply(self, session, ctx, arguments, prepared):
        self.applies += 1
        return ToolResult(
            status="ok",
            sources=[
                {
                    "document_id": "doc-1",
                    "document_name": "Договор.pdf",
                    "page": i,
                    "chunk_id": f"chunk-{i}",
                    "excerpt": "Условия поставки и оплаты. " * 80,
                }
                for i in range(1, 6)
            ],
        )


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_large_russian_retrieval_projection_preserves_five_sources(sessions, protocol):
    op_id = operation(sessions)
    with sessions.begin() as session:
        session.add(
            History(
                operation_id=op_id,
                owner_id=42,
                message={"role": "user", "content": "Что сказано об оплате?"},
            )
        )
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            if protocol == "native":
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call1",
                            "type": "function",
                            "function": {
                                "name": "search_document",
                                "arguments": '{"query":"оплата"}',
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
                            "name": "search_document",
                            "arguments": {"query": "оплата"},
                        }
                    ),
                }
        else:
            assert "tools" not in payload or not payload["tools"]
            content = payload["messages"][-1]["content"]
            assert all(f'"page":{i}' in content for i in range(1, 6))
            message = {
                "role": "assistant",
                "content": "Договор.pdf, стр. 1–5"
                if protocol == "native"
                else '{"type":"final","text":"Договор.pdf, стр. 1–5"}',
            }
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [{"message": message}],
                "usage": {
                    "prompt_tokens": 300,
                    "completion_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            },
        )

    provider = YandexProvider(config(protocol), sessions, httpx.MockTransport(respond))
    registry = Registry()
    tool = SearchTool()
    registry.register("search_document", tool)
    await Runtime(sessions, provider, registry, protocol).run(op_id)
    with sessions() as session:
        op = session.get(Operation, op_id)
        assert op.status == "done"
        assert op.input_spent <= 16000
        assert tool.applies == 1
        invocation = session.scalar(select(Invocation))
        assert len(json.dumps(invocation.result, ensure_ascii=False).encode()) > 20000
        assert len(invocation.model_result["sources"]) == 5
        assert invocation.model_result["truncated"]
        assert usage_totals(session, op_id)["incomplete_calls"] == 0
        assert len(session.scalars(select(AICall)).all()) == 2


async def test_http_retry_unknown_usage_errors_and_budget(sessions):
    op_id = operation(sessions)
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Ответ", "role": "assistant"}}]}
        )

    provider = YandexProvider(config(attempts=2), sessions, httpx.MockTransport(respond))
    await provider.generate(op_id, [{"role": "user", "content": "Вопрос"}], [])
    with sessions() as session:
        events = session.scalars(select(AICall).order_by(AICall.attempt)).all()
        assert len(events) == 2 and events[0].status == "error"
        assert events[0].logical_call_id == events[1].logical_call_id
        assert all(e.input_tokens is None and e.cost is None for e in events)
        assert all(e.latency_ms is not None for e in events)
    with pytest.raises(BudgetExceeded):
        await provider.generate(op_id, [{"role": "user", "content": "я" * 16000}], [])
    with sessions() as session:
        assert len(session.scalars(select(AICall)).all()) == 2


async def test_embeddings_v2_dimensions_usage_and_validation(sessions):
    op_id = operation(sessions)
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        assert payload["dimensions"] == 256
        seen.append(payload["model"])
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [0.5] * 256}],
                "usage": {"prompt_tokens": 100, "total_tokens": 100},
            },
        )

    provider = YandexProvider(config(), sessions, httpx.MockTransport(respond))
    for purpose in ("doc", "query"):
        assert len((await provider.embed(op_id, "Текст", purpose)).vector) == 256
    assert seen == [
        "emb://folder/text-embeddings-v2-doc/",
        "emb://folder/text-embeddings-v2-query/",
    ]
    with sessions() as session:
        assert all(
            e.embedding_tokens == 100 and e.cost_complete
            for e in session.scalars(select(AICall)).all()
        )


class IndependentProvider:
    """A second adapter implements only the public LLMProvider contract."""

    def __init__(self, sessions, invalid=False):
        self.sessions, self.invalid = sessions, invalid
        self.requests = []

    def estimate_request_budget(self, messages, tools):
        return (
            len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode())
            + 256
        )

    async def generate(self, operation_id, messages, tools, lease=None):
        self.requests.append(tools)
        with self.sessions.begin() as session:
            op = session.get(Operation, operation_id)
            op.input_spent += self.estimate_request_budget(messages, tools)
        if tools:
            call = ToolCall(
                str(uuid.uuid4()),
                "invalid" if self.invalid else "search_document",
                {"query": "текст"},
            )
            return Generation("", [call], {"role": "assistant", "content": None}, Usage(), "other")
        return Generation(
            "Готово", [], {"role": "assistant", "content": "Готово"}, Usage(), "other"
        )


async def test_tool_cap_invalid_calls_and_adapter_swap(sessions, monkeypatch):
    monkeypatch.setattr("app.runtime.SYSTEM", "Помощник")
    op_id = operation(sessions)
    provider = IndependentProvider(sessions, invalid=True)
    registry = Registry()
    registry.register("search_document", SearchTool())
    await Runtime(sessions, provider, registry).run(op_id)
    with sessions() as session:
        assert session.get(Operation, op_id).tool_steps == 5
        assert session.get(Operation, op_id).status == "done"
        assert len(session.scalars(select(Invocation)).all()) == 5
        assert provider.requests[-1] == []


async def test_resume_completed_domain_result_without_reapplying(sessions):
    op_id = operation(sessions)
    registry = Registry()
    tool = SearchTool()
    registry.register("search_document", tool)
    with sessions.begin() as session:
        session.get(Operation, op_id).tool_steps = 5
        session.add(
            History(
                operation_id=op_id,
                owner_id=42,
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call1",
                            "type": "function",
                            "function": {"name": "search_document", "arguments": '{"query":"x"}'},
                        }
                    ],
                },
            )
        )
        session.add(
            Invocation(
                operation_id=op_id,
                call_id="call1",
                name="search_document",
                arguments={"query": "x"},
                result=ToolResult(status="ok").model_dump(),
            )
        )
    await Runtime(sessions, IndependentProvider(sessions), registry).run(op_id)
    assert tool.applies == 0
    with sessions() as session:
        assert session.get(Operation, op_id).status == "done"
        assert session.scalar(select(Invocation)).model_result is not None


async def test_provider_failure_never_fabricates_success(sessions):
    op_id = operation(sessions)
    provider = YandexProvider(
        config(), sessions, httpx.MockTransport(lambda _: httpx.Response(401))
    )
    await Runtime(sessions, provider, Registry()).run(op_id)
    with sessions() as session:
        assert session.get(Operation, op_id).status == "error"
        assert "Не удалось" in session.scalar(select(Outbox)).payload["text"]
        assert session.scalar(select(AICall)).error_code == "http_401"


async def test_near_exhausted_budget_forces_final_without_tools(sessions):
    op_id = operation(sessions)
    provider = IndependentProvider(sessions)
    registry = Registry()
    registry.register("search_document", SearchTool())
    with sessions.begin() as session:
        session.get(Operation, op_id).input_spent = 13000
    await Runtime(sessions, provider, registry).run(op_id)
    assert provider.requests == [[]]
    with sessions() as session:
        assert session.get(Operation, op_id).input_spent <= 16000
        assert session.get(Operation, op_id).status == "done"


async def test_invalid_embedding_and_json_shape_are_errors_with_metrics(sessions):
    op_id = operation(sessions)
    provider = YandexProvider(
        config("json"),
        sessions,
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"type":"final","text":"x","extra":true}'}}
                    ]
                },
            )
        ),
    )
    with pytest.raises(ProviderError, match="invalid_generation_response"):
        await provider.generate(op_id, [], [])
    provider = YandexProvider(
        config(),
        sessions,
        httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"embedding": [0.0] * 128}]})
        ),
    )
    with pytest.raises(ProviderError, match="invalid_embedding_response"):
        await provider.embed(op_id, "x")
    with sessions() as session:
        assert len(session.scalars(select(AICall)).all()) == 2


async def test_canonical_approval_summary_is_backend_owned(sessions):
    op_id = operation(sessions)
    with sessions.begin() as session:
        session.get(Operation, op_id).tool_steps = 5
        session.add(
            Invocation(
                operation_id=op_id,
                call_id="approval",
                name="prepare_meeting",
                arguments={},
                result=ToolResult(
                    status="ok",
                    user_message="Встреча 05.10 в 14:00, симуляция",
                    buttons=[[{"text": "Подтвердить", "callback_data": "approve:1"}]],
                ).model_dump(),
                model_result={"status": "ok"},
            )
        )
    await Runtime(sessions, IndependentProvider(sessions), Registry()).run(op_id)
    with sessions() as session:
        item = session.scalar(select(Outbox))
        assert item.payload["text"] == "Встреча 05.10 в 14:00, симуляция"
        assert item.payload["reply_markup"]["inline_keyboard"][0][0]["text"] == "Подтвердить"


@pytest.mark.parametrize("kind", ["generation", "embedding"])
async def test_malformed_success_is_one_failed_billable_attempt(sessions, kind):
    from app.providers import ProviderError

    op_id = operation(sessions)
    body = {
        "choices": [],
        "data": [{"embedding": [0.1] * 128}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }
    provider = YandexProvider(
        config(), sessions, httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    with pytest.raises(ProviderError, match=f"invalid_{kind}_response"):
        if kind == "generation":
            await provider.generate(op_id, [], [])
        else:
            await provider.embed(op_id, "Текст")
    with sessions() as session:
        events = session.scalars(select(AICall)).all()
        assert len(events) == 1
        assert events[0].status == "error"
        assert events[0].error_code == f"invalid_{kind}_response"
        assert events[0].latency_ms is not None and events[0].cost is not None
        assert events[0].extra_usage["prompt_tokens"] == 100


@pytest.mark.parametrize(
    "protocol,content",
    [
        ("json", "[]"),
        ("json", "null"),
        ("json", '"string"'),
        ("native", {"tool_calls": {}}),
        ("native", {"tool_calls": [None]}),
    ],
)
async def test_bad_protocol_immediately_finishes_operation_with_error(sessions, protocol, content):
    op_id = operation(sessions)
    message = {"role": "assistant", "content": content} if protocol == "json" else content
    provider = YandexProvider(
        config(protocol),
        sessions,
        httpx.MockTransport(
            lambda _: httpx.Response(200, json={"choices": [{"message": message}]})
        ),
    )
    await Runtime(sessions, provider, Registry(), protocol).run(op_id)
    with sessions() as session:
        assert session.get(Operation, op_id).status == "error"
        assert "Не удалось" in session.scalar(select(Outbox)).payload["text"]
        events = session.scalars(select(AICall)).all()
        assert len(events) == 1 and events[0].status == "error"
        assert events[0].error_code == "invalid_generation_response"

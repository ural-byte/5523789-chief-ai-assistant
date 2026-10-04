import asyncio
import json
import math
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy import select

from app.config import Settings
from app.models import AICall, Job, Operation
from app.pricing import Pricing, Usage, normalize_usage
from app.queue import require_lease

MAX_INPUT = 16000
MAX_OUTPUT = 2000


class ProviderError(Exception):
    pass


class BudgetExceeded(ProviderError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Generation:
    text: str
    tools: list[ToolCall]
    message: dict
    usage: Usage
    model: str


@dataclass
class Embedding:
    vector: list[float]
    usage: Usage
    model: str


class LLMProvider(Protocol):
    def estimate_request_budget(self, messages, tools) -> int: ...

    async def generate(self, operation_id, messages, tools, lease=None) -> Generation: ...


class EmbeddingProvider(Protocol):
    async def embed(self, operation_id, value, purpose="doc", lease=None) -> Embedding: ...


def wire_bytes(payload):
    # UTF-8 byte count is deliberately conservative for multilingual tokenization.
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) + 256


class YandexProvider:
    def __init__(self, config: Settings, sessions, transport=None):
        self.config = config
        self.sessions = sessions
        self.pricing = Pricing(config.pricing_path)
        self.transport = transport

    def generation_payload(self, messages, tools):
        payload = {
            "model": self.config.model_uri(self.config.generation_model),
            "messages": messages,
            "max_tokens": MAX_OUTPUT,
        }
        if self.config.tool_protocol == "native":
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
        else:
            instruction = {
                "role": "system",
                "content": json.dumps(
                    {
                        "protocol": "Return ONLY JSON: {type:final,text:string} or "
                        "{type:tool,name:string,arguments:object}. "
                        "Use only listed tools. No tools means type:final is required.",
                        "tools": tools,
                    },
                    ensure_ascii=False,
                ),
            }
            payload["messages"] = [instruction, *messages]
            payload["response_format"] = {"type": "json_object"}
        return payload

    def estimate_request_budget(self, messages, tools):
        return wire_bytes(self.generation_payload(messages, tools))

    async def _request(self, operation_id, kind, model, payload, lease=None):
        logical_id = uuid.uuid4()
        usage = Usage()
        for attempt in range(1, self.config.ai_attempts + 1):
            snapshot = self.pricing.snapshot(model)
            with self.sessions.begin() as session:
                if lease:
                    require_lease(session, Job, *lease)
                operation = session.get(Operation, operation_id, with_for_update=True)
                if kind == "generation":
                    size = wire_bytes(payload)
                    if operation.input_spent + size > MAX_INPUT:
                        raise BudgetExceeded("input budget exceeded")
                    operation.input_spent += size
                event = AICall(
                    operation_id=operation_id,
                    logical_call_id=logical_id,
                    attempt=attempt,
                    provider="yandex",
                    model=model,
                    operation_type=kind,
                    scenario=operation.scenario,
                    currency=snapshot["currency"],
                    pricing_version=snapshot["version"],
                    pricing_snapshot=snapshot,
                )
                session.add(event)
                session.flush()
                event_id = event.id
            started = time.monotonic()
            error = None
            response_data = None
            actual_model = model
            usage = Usage()
            retry = False
            try:
                if not self.config.ai_api_key.get_secret_value() or not self.config.ai_folder_id:
                    raise ProviderError("provider_not_configured")
                async with httpx.AsyncClient(
                    transport=self.transport, timeout=self.config.ai_timeout_seconds
                ) as client:
                    response = await client.post(
                        self.config.ai_base_url.rstrip("/")
                        + ("/chat/completions" if kind == "generation" else "/embeddings"),
                        json=payload,
                        headers={
                            "Authorization": "Api-Key " + self.config.ai_api_key.get_secret_value(),
                            "OpenAI-Project": self.config.ai_folder_id,
                        },
                    )
                retry = response.status_code == 429 or response.status_code >= 500
                if response.is_error:
                    raise ProviderError(f"http_{response.status_code}")
                response_data = response.json()
                if not isinstance(response_data, dict):
                    raise ProviderError("invalid_response_body")
                usage = normalize_usage(response_data.get("usage"), kind == "embedding")
                actual_model = response_data.get("model", model)
                if not isinstance(actual_model, str):
                    raise ProviderError("invalid_response_model")
            except (httpx.HTTPError, ValueError, ProviderError) as exc:
                error = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
                retry = retry or isinstance(exc, httpx.TransportError)
                actual_model = model
            finally:
                with self.sessions.begin() as session:
                    event = session.get(AICall, event_id)
                    event.status = "error" if error else "succeeded"
                    event.latency_ms = round((time.monotonic() - started) * 1000)
                    event.error_code = error
                    event.model = actual_model
                    event.input_tokens = usage.input_tokens
                    event.output_tokens = usage.output_tokens
                    event.cached_tokens = usage.cached_tokens
                    event.tool_tokens = usage.tool_tokens
                    event.embedding_tokens = usage.embedding_tokens
                    event.extra_usage = usage.extras
                    snapshot = self.pricing.snapshot(actual_model)
                    event.pricing_snapshot = snapshot
                    event.cost, event.cost_complete = self.pricing.calculate(
                        usage, snapshot, kind == "embedding"
                    )
            if error:
                if retry and attempt < self.config.ai_attempts:
                    await asyncio.sleep(attempt)
                    continue
                raise ProviderError(error)
            return response_data, usage, actual_model, event_id
        raise ProviderError("attempt_limit")

    def _invalid_response(self, event_id, code):
        # Keep the usage, latency and price of the actual HTTP attempt: it may be billable
        # even though the response cannot be used. Never create a second call event.
        with self.sessions.begin() as session:
            event = session.get(AICall, event_id)
            event.status = "error"
            event.error_code = code

    async def generate(self, operation_id, messages, tools, lease=None):
        payload = self.generation_payload(messages, tools)
        raw, usage, model, event_id = await self._request(
            operation_id, "generation", payload["model"], payload, lease
        )
        try:
            if not isinstance(raw.get("choices"), list):
                raise ValueError("choices must be array")
            message = raw["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError("invalid message")
            message = {"role": "assistant", **message}
            calls = []
            if self.config.tool_protocol == "native":
                container = message.get("tool_calls")
                if container is None:
                    container = []
                if not isinstance(container, list):
                    raise ValueError("tool_calls must be array")
                for call in container:
                    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                        raise ValueError("tool call must be object")
                    if not isinstance(call.get("id"), str) or not call["id"]:
                        raise ValueError("tool call ID required")
                    if not isinstance(call["function"].get("name"), str):
                        raise ValueError("function name must be string")
                    arguments = json.loads(call["function"]["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be object")
                    calls.append(ToolCall(call["id"], call["function"]["name"], arguments))
                text_value = message.get("content") or ""
                if not isinstance(text_value, str):
                    raise ValueError("content must be string")
            else:
                parsed = json.loads(message["content"])
                if not isinstance(parsed, dict):
                    raise ValueError("JSON protocol must be object")
                if parsed.get("type") == "tool" and set(parsed) == {"type", "name", "arguments"}:
                    if not isinstance(parsed["arguments"], dict) or not isinstance(
                        parsed["name"], str
                    ):
                        raise ValueError("arguments must be object")
                    calls = [ToolCall(str(uuid.uuid4()), parsed["name"], parsed["arguments"])]
                    text_value = ""
                elif parsed.get("type") == "final" and set(parsed) == {"type", "text"}:
                    text_value = parsed["text"]
                    if not isinstance(text_value, str):
                        raise ValueError("text must be string")
                else:
                    raise ValueError("invalid JSON protocol")
            return Generation(text_value, calls, message, usage, model)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            self._invalid_response(event_id, "invalid_generation_response")
            raise ProviderError("invalid_generation_response") from exc

    async def embed(self, operation_id, value, purpose="doc", lease=None):
        if purpose not in {"doc", "query"}:
            raise ValueError("embedding purpose")
        suffix = (
            self.config.embedding_doc_model
            if purpose == "doc"
            else self.config.embedding_query_model
        )
        model_uri = self.config.model_uri(suffix, embedding=True)
        raw, usage, model, event_id = await self._request(
            operation_id,
            "embedding",
            model_uri,
            {"model": model_uri, "input": value, "dimensions": 256},
            lease,
        )
        try:
            vector = raw["data"][0]["embedding"]
            if (
                not isinstance(vector, list)
                or len(vector) != 256
                or any(
                    not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x)
                    for x in vector
                )
            ):
                raise ValueError("invalid vector")
            return Embedding(vector, usage, model)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            self._invalid_response(event_id, "invalid_embedding_response")
            raise ProviderError("invalid_embedding_response") from exc


def usage_totals(session, operation_id):
    events = session.scalars(select(AICall).where(AICall.operation_id == operation_id)).all()
    return {
        "calls": len(events),
        "known_cost": str(sum((event.cost or 0 for event in events))),
        "incomplete_calls": sum(not event.cost_complete for event in events),
        "currencies": sorted({event.currency for event in events}),
        "tokens": {
            field: {
                "known": sum(getattr(e, field) or 0 for e in events),
                "unknown_calls": sum(getattr(e, field) is None for e in events),
            }
            for field in (
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "tool_tokens",
                "embedding_tokens",
            )
        },
    }

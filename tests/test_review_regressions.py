"""User-facing PDF selection, ordered tool batches and deterministic action replies."""

import json
import uuid

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.config import Settings
from app.domain_actions import Approval, CreateTask, PrepareMeeting, Task
from app.domain_memory import Document, SearchDocument
from app.models import History, Invocation, Operation, Outbox, Update
from app.providers import MAX_INPUT, YandexProvider
from app.runtime import Runtime
from app.tools import Registry, ToolResult
from tests.test_storage import operation


def input_operation(sessions, text):
    op_id = operation(sessions)
    with sessions.begin() as session:
        update = Update(
            id=uuid.uuid4().int % 900000000000000000,
            owner_id=42,
            payload={"message": {"text": text}},
        )
        session.add(update)
        session.flush()
        session.get(Operation, op_id).update_id = update.id
        session.add(
            History(operation_id=op_id, owner_id=42, message={"role": "user", "content": text})
        )
    return op_id


def provider(sessions, protocol, respond):
    def transport(request):
        payload = json.loads(request.content)
        return respond(payload)

    return YandexProvider(
        Settings(
            _env_file=None,
            ai_api_key="test",
            ai_folder_id="folder",
            ai_attempts=1,
            tool_protocol=protocol,
        ),
        sessions,
        httpx.MockTransport(transport),
    )


def response(protocol, calls=(), text="Готово"):
    if calls and protocol == "native":
        message = {
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
                for call_id, name, args in calls
            ],
        }
    else:
        content = (
            {"type": "tool", "name": calls[0][1], "arguments": calls[0][2]}
            if calls
            else {"type": "final", "text": text}
        )
        message = {
            "content": json.dumps(content, ensure_ascii=False) if protocol == "json" else text
        }
    return httpx.Response(
        200,
        json={
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        },
    )


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_pdf_selection_delivers_names_without_internal_ids(sessions, protocol):
    for name in ("Бюджет.pdf", "Договор.pdf"):
        doc_op = operation(sessions)
        with sessions.begin() as session:
            session.add(
                Document(
                    operation_id=doc_op,
                    owner_id=42,
                    name=name,
                    file_path=f"{doc_op}.pdf",
                    size=100,
                    status="ready",
                )
            )
    op_id = input_operation(sessions, "Какой бюджет в PDF?")
    calls = []

    def respond(payload):
        calls.append(payload)
        return response(
            protocol,
            [("pdf", "search_document", {"query": "бюджет"})] if len(calls) == 1 else (),
            text="Уточните документ.",
        )

    registry = Registry()
    registry.register("search_document", SearchDocument(sessions, None))
    await Runtime(sessions, provider(sessions, protocol, respond), registry, protocol).run(op_id)
    with sessions() as session:
        assert session.get(Operation, op_id).status == "done"
        outputs = session.scalars(select(Outbox).where(Outbox.operation_id == op_id)).all()
        assert len(outputs) == 1
        assert "Бюджет.pdf" in outputs[0].payload["text"]
        assert "Договор.pdf" in outputs[0].payload["text"]
        for document in session.scalars(select(Document)):
            assert str(document.id) not in outputs[0].payload["text"]


@pytest.mark.parametrize("restart", [False, True])
async def test_tool_batch_ordinals_preserve_execution_and_recovery_order(sessions, restart):
    class Arguments(BaseModel):
        query: str

    applied = []

    class Tool:
        arguments = Arguments
        description = "Test retrieval"

        async def prepare(self, ctx, args):
            return args.query

        def apply(self, session, ctx, args, prepared):
            applied.append(prepared)
            return ToolResult(status="ok", data={"query": prepared})

    op_id = input_operation(sessions, "Найди данные")
    batches = iter((("c1", "c2", "c3"), ("c4",), ("c5",), ()))

    def respond(payload):
        return response(
            "native", [(name, "search_document", {"query": name}) for name in next(batches)]
        )

    registry = Registry()
    registry.register("search_document", Tool())
    ai = provider(sessions, "native", respond)

    class Interrupted(Runtime):
        async def _invoke(self, op, inv, messages, lease, legacy_pending=False):
            if inv.call_id == "c4":
                raise RuntimeError("simulated interruption before apply")
            return await super()._invoke(op, inv, messages, lease, legacy_pending)

    if restart:
        with pytest.raises(RuntimeError, match="simulated interruption"):
            await Interrupted(sessions, ai, registry).run(op_id)
    await Runtime(sessions, ai, registry).run(op_id)
    with sessions() as session:
        rows = session.scalars(
            select(Invocation).where(Invocation.operation_id == op_id).order_by(Invocation.ordinal)
        ).all()
        assert [row.call_id for row in rows] == ["c1", "c2", "c3", "c4", "c5"]
        assert [row.ordinal for row in rows] == [0, 1, 2, 3, 4]
        assert applied == ["c1", "c2", "c3", "c4", "c5"]
        assert session.get(Operation, op_id).status == "done"


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize(
    "name,handler", [("create_task", CreateTask), ("prepare_meeting", PrepareMeeting)]
)
@pytest.mark.parametrize("precise", [True, False])
async def test_single_action_finishes_without_projection_or_final_generation(
    sessions, protocol, name, handler, precise
):
    phrase = "через два часа" if precise else "вечером"
    op_id = input_operation(sessions, f"Подготовь действие {phrase}")
    requests = []

    def respond(payload):
        requests.append(payload)
        assert len(requests) == 1, "canonical result must not need another model call"
        # A completed action must remain deliverable even without a projection budget.
        with sessions.begin() as session:
            session.get(Operation, op_id).input_spent = MAX_INPUT
        return response(
            protocol,
            [
                (
                    "action",
                    name,
                    {
                        "text": "Позвонить Иванову",
                        "complete_request": True,
                        "date": {
                            "kind": "relative",
                            "source_phrase": phrase,
                            "amount": 2,
                            "unit": "hours",
                        },
                    },
                )
            ],
        )

    registry = Registry()
    registry.register(name, handler())
    ai = provider(sessions, protocol, respond)
    await Runtime(sessions, ai, registry, protocol).run(op_id)
    await Runtime(sessions, ai, registry, protocol).run(op_id)
    with sessions() as session:
        op = session.get(Operation, op_id)
        assert op.status == "done" and op.error_reason is None
        outputs = session.scalars(select(Outbox).where(Outbox.operation_id == op_id)).all()
        assert len(outputs) == 1
        result = session.scalar(select(Invocation).where(Invocation.operation_id == op_id)).result
        assert outputs[0].payload["text"] == result["user_message"]
        assert (
            outputs[0].payload.get("reply_markup", {}).get("inline_keyboard", [])
            == result["buttons"]
        )
        assert result["status"] == ("ok" if precise else "needs_clarification")
        assert len(session.scalars(select(Task)).all()) == int(precise and name == "create_task")
        approvals = session.scalars(select(Approval)).all()
        assert len(approvals) == int(precise and name == "prepare_meeting")
        assert all(row.status == "pending" for row in approvals)
        final = session.scalars(
            select(History).where(History.operation_id == op_id).order_by(History.id)
        ).all()[-1]
        assert final.message == {"role": "assistant", "content": result["user_message"]}
    assert len(requests) == 1


async def test_action_without_completion_signal_allows_remaining_actions(sessions):
    op_id = input_operation(sessions, "Напомни через два часа позвонить и написать Иванову")
    requests = []

    def respond(payload):
        requests.append(payload)
        if len(requests) > 2:
            return response("native", text="Поручения созданы.")
        # The first call intentionally omits the optional signal, as older callers do.
        args = {
            "text": "Позвонить" if len(requests) == 1 else "Написать",
            "date": {
                "kind": "relative",
                "source_phrase": "через два часа",
                "amount": 2,
                "unit": "hours",
            },
        }
        if len(requests) == 2:
            args["complete_request"] = True
        return response("native", [(f"action{len(requests)}", "create_task", args)])

    registry = Registry()
    registry.register("create_task", CreateTask())
    await Runtime(sessions, provider(sessions, "native", respond), registry).run(op_id)
    with sessions() as session:
        assert session.get(Operation, op_id).status == "done"
        assert {row.text for row in session.scalars(select(Task))} == {"Позвонить", "Написать"}
    assert len(requests) == 3


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_committed_action_recovers_without_repeat_or_final_generation(sessions, protocol):
    op_id = input_operation(sessions, "Напомни через два часа позвонить Иванову")
    requests = []

    def respond(payload):
        requests.append(payload)
        assert len(requests) == 1
        return response(
            protocol,
            [
                (
                    "action",
                    "create_task",
                    {
                        "text": "Позвонить Иванову",
                        "complete_request": True,
                        "date": {
                            "kind": "relative",
                            "source_phrase": "через два часа",
                            "amount": 2,
                            "unit": "hours",
                        },
                    },
                )
            ],
        )

    class Interrupted(Runtime):
        def _complete_action(self, *args):
            raise RuntimeError("simulated interruption after action commit")

    registry = Registry()
    registry.register("create_task", CreateTask())
    ai = provider(sessions, protocol, respond)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await Interrupted(sessions, ai, registry, protocol).run(op_id)
    await Runtime(sessions, ai, registry, protocol).run(op_id)
    with sessions() as session:
        assert session.get(Operation, op_id).status == "done"
        assert len(session.scalars(select(Task)).all()) == 1
        assert len(session.scalars(select(Outbox).where(Outbox.operation_id == op_id)).all()) == 1
    assert len(requests) == 1


@pytest.mark.parametrize("protocol", ["native", "json"])
def test_compact_action_schemas_keep_date_constraints_and_completion_signal(sessions, protocol):
    op_id = operation(sessions)
    registry = Registry()
    registry.register("create_task", CreateTask())
    registry.register("prepare_meeting", PrepareMeeting())
    ai = provider(sessions, protocol, lambda payload: None)
    original = registry.schemas()
    with sessions() as session:
        compact = Runtime(sessions, ai, registry, protocol)._schemas(
            session, session.get(Operation, op_id)
        )
    for before, after in zip(original, compact, strict=True):
        before = before["function"]["parameters"]
        after = after["function"]["parameters"]
        assert before["required"] == after["required"]
        assert before["additionalProperties"] == after["additionalProperties"]
        assert before["properties"].keys() == after["properties"].keys()
        assert after["properties"]["complete_request"]["default"] is False
        assert before["properties"]["date"]["oneOf"] == after["properties"]["date"]["oneOf"]
        for name, definition in before["$defs"].items():
            assert definition["required"] == after["$defs"][name]["required"]
            for field, constraint in definition["properties"].items():
                assert {k: v for k, v in constraint.items() if k != "title"} == after["$defs"][
                    name
                ]["properties"][field]
    assert ai.estimate_request_budget([], compact) < ai.estimate_request_budget([], original)

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.dates import AbsoluteDate, RelativeDate, UnresolvedDate, WeekdayDate, resolve_date
from app.domain_actions import (
    ActionArgs,
    Approval,
    CreateTask,
    PrepareMeeting,
    Task,
    handle_callback,
    scheduler_loop,
    scheduler_once,
)
from app.models import Invocation, Job, Outbox, now
from app.queue import claim
from app.tools import ToolContext
from tests.test_storage import operation

REFERENCE = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "spec,expected",
    [
        (
            AbsoluteDate(
                kind="absolute",
                source_phrase="завтра в 15:00",
                local_date="2025-01-01",
                local_time="15:00",
            ),
            datetime(2026, 10, 5, 12, tzinfo=UTC),
        ),
        (
            WeekdayDate(
                kind="weekday", source_phrase="в пятницу в 10:00", weekday=4, local_time="10:00"
            ),
            datetime(2026, 10, 9, 7, tzinfo=UTC),
        ),
        (
            RelativeDate(kind="relative", source_phrase="через два часа", amount=2, unit="hours"),
            datetime(2026, 10, 4, 11, tzinfo=UTC),
        ),
    ],
)
def test_unambiguous_dates_no_questions(spec, expected):
    actual, question = resolve_date(spec, REFERENCE, "Europe/Moscow", spec.source_phrase)
    assert question is None and actual == expected


@pytest.mark.parametrize("phrase", ["вечером", "после обеда", "через несколько дней"])
def test_vague_cannot_be_silently_normalized(phrase):
    spec = AbsoluteDate(
        kind="absolute", source_phrase=phrase, local_date="2026-10-05", local_time="19:00"
    )
    assert resolve_date(spec, REFERENCE, "Europe/Moscow", phrase)[0] is None


def context(sessions, text, owner=42):
    op_id = operation(sessions, owner)
    with sessions.begin() as session:
        row = Invocation(
            operation_id=op_id,
            call_id=str(uuid.uuid4()),
            ordinal=0,
            name="create_task",
            arguments={},
        )
        session.add(row)
        session.flush()
        invocation_id = row.id
    return ToolContext(
        owner,
        owner,
        op_id,
        REFERENCE,
        "Europe/Moscow",
        str(invocation_id),
        {"message": {"text": text}},
    )


async def test_task_persistence_duplicate_and_overdue_notification(sessions):
    ctx = context(sessions, "Напомни через два часа позвонить Иванову")
    args = ActionArgs(
        text="Позвонить Иванову",
        date=RelativeDate(kind="relative", source_phrase="через два часа", amount=2, unit="hours"),
    )
    handler = CreateTask()
    prepared = await handler.prepare(ctx, args)
    with sessions.begin() as session:
        first = handler.apply(session, ctx, args, prepared)
        second = handler.apply(session, ctx, args, prepared)
        assert first.data == second.data
    deadline = REFERENCE + timedelta(hours=2)
    scheduler_once(sessions, 99, deadline + timedelta(minutes=5))
    scheduler_once(sessions, 42, deadline + timedelta(minutes=5))
    scheduler_once(sessions, 42, deadline + timedelta(minutes=5))
    with sessions() as session:
        assert len(session.scalars(select(Task)).all()) == 1
        assert session.scalar(select(Task)).status == "notified"
        messages = session.scalars(select(Outbox)).all()
        assert len(messages) == 1 and "задержкой 300" in messages[0].payload["text"]


async def callback(sessions, approval_id, owner=42, choice="y"):
    op_id = operation(sessions, owner)
    payload = {"callback_query": {"id": str(uuid.uuid4()), "data": f"a:{approval_id}:{choice}"}}
    with sessions.begin() as session:
        session.add(Job(key=str(op_id), operation_id=op_id, kind="callback", payload=payload))
    with sessions.begin() as session:
        job = claim(session, Job, owner)
    await handle_callback(sessions, job, (job.id, job.lease_token))
    with sessions.begin() as session:
        session.get(Job, job.id).status = "done"


async def approval(sessions):
    ctx = context(sessions, "Встреча завтра в 14:00")
    args = ActionArgs(
        text="Встреча",
        date=AbsoluteDate(
            kind="absolute",
            source_phrase="завтра в 14:00",
            local_date="2026-10-05",
            local_time="14:00",
        ),
    )
    handler = PrepareMeeting()
    prepared = await handler.prepare(ctx, args)
    with sessions.begin() as session:
        result = handler.apply(session, ctx, args, prepared)
    assert result.presentation == "canonical" and "14:00" in result.user_message
    return uuid.UUID(result.data["approval_id"])


async def test_approval_requires_owner_and_confirmation_and_repeated_click(sessions):
    approval_id = await approval(sessions)
    with sessions() as session:
        original = session.get(Approval, approval_id).payload.copy()
        assert session.get(Approval, approval_id).status == "pending"
    await callback(sessions, approval_id, owner=99)
    with sessions() as session:
        assert session.get(Approval, approval_id).status == "pending"
    await callback(sessions, approval_id)
    with sessions() as session:
        executed = session.get(Approval, approval_id).executed_at
    await callback(sessions, approval_id)
    with sessions() as session:
        row = session.get(Approval, approval_id)
        assert row.status == "simulated" and row.payload == original
        assert row.executed_at == executed and row.approved_at is not None


@pytest.mark.parametrize("expired", [False, True])
async def test_cancel_or_expire_never_executes(sessions, expired):
    approval_id = await approval(sessions)
    if expired:
        with sessions.begin() as session:
            session.get(Approval, approval_id).expires_at = now() - timedelta(seconds=1)
    await callback(sessions, approval_id, choice="y" if expired else "n")
    with sessions() as session:
        row = session.get(Approval, approval_id)
        assert row.status == ("expired" if expired else "cancelled")
        assert row.executed_at is None


async def test_scheduler_runs_while_agent_waits_for_network(sessions):
    ctx = context(sessions, "Напомни через два часа")
    args = ActionArgs(
        text="Важное поручение",
        date=RelativeDate(kind="relative", source_phrase="через два часа", amount=2, unit="hours"),
    )
    handler = CreateTask()
    with sessions.begin() as session:
        handler.apply(session, ctx, args, (now() - timedelta(seconds=1), None))
    waiting = asyncio.create_task(asyncio.sleep(10))
    scheduler = asyncio.create_task(scheduler_loop(sessions, 42))
    try:
        for _ in range(30):
            await asyncio.sleep(0.05)
            with sessions() as session:
                if session.scalar(select(Task)).status == "notified":
                    break
        with sessions() as session:
            assert session.scalar(select(Task)).status == "notified"
        assert not waiting.done()
    finally:
        waiting.cancel()
        scheduler.cancel()
        await asyncio.gather(waiting, scheduler, return_exceptions=True)


async def test_update_runtime_creates_one_task_and_canonical_reply_despite_llm_failure(
    sessions, monkeypatch
):
    import json

    import httpx
    from fastapi.testclient import TestClient

    from app import api
    from app.config import Settings
    from app.providers import YandexProvider
    from app.runtime import Runtime
    from app.tools import Registry

    config = Settings(
        service_token="test",
        allowed_telegram_user_id=42,
        ai_api_key="test",
        ai_folder_id="folder",
        ai_attempts=1,
    )
    monkeypatch.setattr(api, "settings", lambda: config)
    monkeypatch.setattr(api, "session_factory", lambda: sessions)
    update = {
        "update_id": 100,
        "message": {
            "from": {"id": 42},
            "chat": {"id": 42},
            "date": int(REFERENCE.timestamp()),
            "text": "Напомни через два часа позвонить Иванову",
        },
    }
    client = TestClient(api.app)
    headers = {"Authorization": "Bearer test"}
    op_id = uuid.UUID(
        client.post("/internal/updates", json=update, headers=headers).json()["operation_id"]
    )
    assert client.post("/internal/updates", json=update, headers=headers).json()["duplicate"]
    requests = []

    def reply(request):
        requests.append(request)
        if len(requests) > 1:
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "function": {
                                        "name": "create_task",
                                        "arguments": json.dumps(
                                            {
                                                "text": "Позвонить Иванову",
                                                "date": {
                                                    "kind": "relative",
                                                    "source_phrase": "через два часа",
                                                    "amount": 2,
                                                    "unit": "hours",
                                                },
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    registry = Registry()
    registry.register("create_task", CreateTask())
    runtime = Runtime(
        sessions, YandexProvider(config, sessions, httpx.MockTransport(reply)), registry
    )
    await runtime.run(op_id)
    await runtime.run(op_id)
    with sessions() as session:
        rows = session.scalars(select(Task)).all()
        assert len(rows) == 1 and rows[0].deadline == REFERENCE + timedelta(hours=2)
        messages = [row.payload["text"] for row in session.scalars(select(Outbox)).all()]
        assert any("Поручение создано" in item for item in messages)
        assert any("Не удалось" in item for item in messages)


def test_model_cannot_omit_vague_time_or_change_literal_values():
    guessed = AbsoluteDate(
        kind="absolute", source_phrase="Напомни", local_date="2026-10-05", local_time="19:00"
    )
    assert resolve_date(guessed, REFERENCE, "Europe/Moscow", "Напомни вечером позвонить")[0] is None
    wrong_clock = AbsoluteDate(
        kind="absolute", source_phrase="завтра в 15:00", local_date="2026-10-05", local_time="18:00"
    )
    assert resolve_date(wrong_clock, REFERENCE, "Europe/Moscow", "завтра в 15:00")[0] == datetime(
        2026, 10, 5, 12, tzinfo=UTC
    )
    wrong_interval = RelativeDate(
        kind="relative", source_phrase="через два часа", amount=99, unit="days"
    )
    assert resolve_date(wrong_interval, REFERENCE, "Europe/Moscow", "через два часа")[
        0
    ] == REFERENCE + timedelta(hours=2)


@pytest.mark.parametrize("hour,expected_day", [(6, 9), (8, 16)])
def test_friday_nearest_suitable_including_same_day(hour, expected_day):
    spec = WeekdayDate(
        kind="weekday", source_phrase="в пятницу в 10:00", weekday=1, local_time="18:00"
    )
    actual, question = resolve_date(
        spec, datetime(2026, 10, 9, hour, tzinfo=UTC), "Europe/Moscow", spec.source_phrase
    )
    assert question is None and actual == datetime(2026, 10, expected_day, 7, tzinfo=UTC)


def test_clear_source_overrides_excessively_cautious_model_and_supports_calendar_dates():
    for phrase, expected in [
        ("завтра в 15:00", datetime(2026, 10, 5, 12, tzinfo=UTC)),
        ("через полчаса", REFERENCE + timedelta(minutes=30)),
        ("5 октября в 15:00", datetime(2026, 10, 5, 12, tzinfo=UTC)),
    ]:
        spec = UnresolvedDate(kind="unresolved", source_phrase=phrase, question="Уточните время")
        assert resolve_date(spec, REFERENCE, "Europe/Moscow", phrase) == (expected, None)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("Напомни завтра в 15:00 позвонить Иванову", datetime(2026, 10, 5, 12, tzinfo=UTC)),
        ("Напомни в пятницу в 10:00 позвонить Иванову", datetime(2026, 10, 9, 7, tzinfo=UTC)),
    ],
)
def test_partial_model_clock_does_not_drop_original_calendar_anchor(source, expected):
    phrase = "в 15:00" if "15:00" in source else "в 10:00"
    spec = AbsoluteDate(
        kind="absolute", source_phrase=phrase, local_date="2026-10-04", local_time="18:00"
    )
    assert resolve_date(spec, REFERENCE, "Europe/Moscow", source) == (expected, None)

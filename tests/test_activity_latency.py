import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import api
from app.bootstrap import install
from app.config import Settings
from app.latency import finish_operation, operation_latency
from app.memory_overview import memory_page, truncate
from app.models import Activity, AICall, Operation, Outbox, now
from app.queue import acknowledge, claim, enqueue_text
from app.workers import activity_once, background_once, deliver_once, poll_once
from tests.test_data_controls import memory
from tests.test_memory_documents import Embeddings
from tests.test_storage import operation


def client(sessions, monkeypatch):
    config = Settings(allowed_telegram_user_id=42, service_token="test")
    monkeypatch.setattr(api, "settings", lambda: config)
    monkeypatch.setattr(api, "session_factory", lambda: sessions)
    return TestClient(api.app), {"Authorization": "Bearer test"}, config


def ingest(client, headers, text="Что ты сейчас обо мне помнишь?", update=1):
    return uuid.UUID(
        client.post(
            "/internal/updates",
            headers=headers,
            json={
                "update_id": update,
                "message": {
                    "date": int(now().timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42},
                    "text": text,
                },
            },
        ).json()["operation_id"]
    )


async def test_overview_ingress_zero_ai_empty_paging_owner_and_truncation(sessions, monkeypatch):
    web, headers, config = client(sessions, monkeypatch)
    install(sessions, Embeddings(), config)
    opid = ingest(web, headers)
    await background_once(sessions, Embeddings(), config)
    with sessions() as session:
        assert session.get(Operation, opid).status == "done"
        assert session.get(Operation, opid).retrieval_ms is not None
        assert not session.scalar(select(AICall).where(AICall.operation_id == opid))
        assert (
            "пока нет"
            in session.scalar(select(Outbox).where(Outbox.operation_id == opid)).payload["text"]
        )
    created = []
    for i in range(21):
        mid, _ = await memory(sessions, str(i) + "👩" * 2000)
        created.append(mid)
    foreign, _ = await memory(sessions, "foreign", 99)
    opid = operation(sessions)
    with sessions.begin() as session:
        op = session.get(Operation, opid)
        first, buttons = memory_page(session, op)
        assert "На этой странице: 20" in first and buttons
        cursor = uuid.UUID(buttons[0][0]["callback_data"][2:])
        second, buttons = memory_page(session, op, cursor)
        assert "На этой странице: 1" in second and not buttons
        rejected, _ = memory_page(session, op, foreign)
        assert "недоступна" in rejected and "foreign" not in rejected
    assert len(truncate("😀" * 3000).encode("utf-16-le")) // 2 <= 3500
    assert "сокращена" in first


async def test_activity_immediate_renewal_once_progress_terminal_duplicate_and_receipt(
    sessions, monkeypatch
):
    web, headers, _ = client(sessions, monkeypatch)
    opid = ingest(web, headers, "долгий вопрос")
    first = web.post("/internal/activity/claim", headers=headers).json()
    assert len(first) == 1 and first[0]["operation_id"] == str(opid)
    assert not web.post("/internal/activity/claim", headers=headers).json()
    with sessions.begin() as session:
        activity = session.get(Activity, opid)
        activity.next_typing_at = now() - timedelta(seconds=1)
        activity.progress_at = now() - timedelta(seconds=1)
    assert len(web.post("/internal/activity/claim", headers=headers).json()) == 1
    with sessions.begin() as session:
        session.get(Activity, opid).next_typing_at = now() - timedelta(seconds=1)
    web.post("/internal/activity/claim", headers=headers)
    with sessions.begin() as session:
        progress = session.scalars(select(Outbox).where(Outbox.purpose == "progress")).all()
        assert len(progress) == 1 and "ещё работаю" in progress[0].payload["text"]
        op = session.get(Operation, opid)
        op.status = "done"
        enqueue_text(session, op, "готово")
        finish_operation(session, op)
    with sessions.begin() as session:
        session.get(Activity, opid).next_typing_at = now() - timedelta(seconds=1)
        progress = session.scalar(select(Outbox).where(Outbox.purpose == "progress"))
        assert progress.status == "pending"
    assert web.post("/internal/activity/claim", headers=headers).json()
    with sessions.begin() as session:
        row = session.scalar(select(Outbox).where(Outbox.purpose == "final"))
        row.status = "running"
        row.lease_token = uuid.uuid4()
        row.lease_until = now() + timedelta(minutes=2)
        acknowledge(session, Outbox, row.id, row.lease_token)
    assert not web.post("/internal/activity/claim", headers=headers).json()
    with sessions() as session:
        assert (
            session.scalar(select(Outbox).where(Outbox.purpose == "progress")).status == "cancelled"
        )
    web.post(
        "/internal/updates",
        headers=headers,
        json={
            "update_id": 1,
            "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "duplicate"},
        },
    )
    with sessions() as session:
        assert len(session.scalars(select(Activity)).all()) == 1


async def test_delivery_ack_measurement_final_chunks_exclude_notification(sessions):
    opid = operation(sessions)
    with sessions.begin() as session:
        op = session.get(Operation, opid)
        op.received_at = now() - timedelta(seconds=4)
        op.source_message_at = op.received_at - timedelta(seconds=2)
        op.first_started_at = op.received_at + timedelta(seconds=1)
        op.processing_finished_at = op.received_at + timedelta(seconds=2)
        op.status = "done"
        enqueue_text(session, op, "😀" * 3000)
        enqueue_text(
            session, op, "future reminder", key_prefix="notification", purpose="notification"
        )
    with sessions.begin() as session:
        one = claim(session, Outbox, 42)
        acknowledge(session, Outbox, one.id, one.lease_token)
        assert session.get(Operation, opid).final_delivery_ack_at is None
    with sessions.begin() as session:
        two = claim(session, Outbox, 42)
        acknowledge(session, Outbox, two.id, two.lease_token)
        op = session.get(Operation, opid)
        assert op.final_delivery_ack_at
        before = op.final_delivery_ack_at
        metrics = operation_latency(session, op)
        assert metrics["queue_wait_ms"] == 1000 and metrics["runtime_ms"] == 1000
        assert metrics["source_to_ingress_ms"] == 2000 and metrics["ai_calls"] == 0
        assert metrics["end_to_end_ms"] >= 4000 and metrics["source_to_ack_ms"] >= 6000
    with sessions.begin() as session:
        notification = claim(session, Outbox, 42)
        acknowledge(session, Outbox, notification.id, notification.lease_token)
        assert session.get(Operation, opid).final_delivery_ack_at == before


async def test_longpoll_stall_does_not_block_delivery_or_activity():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def bot_response(request):
        calls.append(request.url.path)
        if request.url.path.endswith("getUpdates"):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})

    def backend_response(request):
        if request.url.path.endswith("/checkpoint"):
            return httpx.Response(200, json={"value": 1})
        if request.url.path.endswith("/outbox/claim"):
            return httpx.Response(
                200,
                json={
                    "id": "outbox",
                    "lease_token": str(uuid.uuid4()),
                    "kind": "sendMessage",
                    "payload": {"chat_id": 42, "text": "reply"},
                },
            )
        if request.url.path.endswith("/activity/claim"):
            return httpx.Response(200, json=[{"operation_id": str(uuid.uuid4()), "chat_id": 42}])
        return httpx.Response(200, json={"ok": True})

    async with (
        httpx.AsyncClient(
            base_url="https://example.invalid/", transport=httpx.MockTransport(bot_response)
        ) as bot,
        httpx.AsyncClient(
            base_url="http://backend/", transport=httpx.MockTransport(backend_response)
        ) as backend,
    ):
        poll = asyncio.create_task(poll_once(backend, bot))
        await entered.wait()
        await asyncio.wait_for(
            asyncio.gather(deliver_once(backend, bot), activity_once(backend, bot)), timeout=1
        )
        assert not poll.done()
        assert any(p.endswith("sendMessage") for p in calls)
        assert any(p.endswith("sendChatAction") for p in calls)
        release.set()
        await poll


@pytest.mark.parametrize(
    "text",
    [
        "У меня нет такого инструмента",
        "Удалите вручную в файловом хранилище",
        "Операция в backend queue LLM недоступна",
    ],
)
def test_product_reply_avoids_infrastructure_in_unsupported_flow(text):
    from app.runtime import product_reply

    result = product_reply(text)
    assert "/reset" in result and "подтверждения" in result
    assert "backend" not in result and "инструмента" not in result


def test_product_policy_preserves_user_technical_facts():
    from app.runtime import product_reply

    text = "Вы работаете над проектом LLM и изучаете backend-разработку."
    assert product_reply(text) == text

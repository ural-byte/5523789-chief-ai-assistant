from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import api
from app.config import Settings
from app.models import History, Job, Operation, Outbox, Update, now
from app.queue import LeaseLost, acknowledge, claim, enqueue_text, renew


def operation(sessions, owner=42):
    with sessions.begin() as session:
        op = Operation(owner_id=owner, chat_id=owner, timezone="Europe/Moscow")
        session.add(op)
        session.flush()
        return op.id


def test_durable_ingestion_auth_isolation_duplicate(sessions, monkeypatch):
    config = Settings(service_token="test", allowed_telegram_user_id=42)
    monkeypatch.setattr(api, "settings", lambda: config)
    monkeypatch.setattr(api, "session_factory", lambda: sessions)
    client = TestClient(api.app)
    payload = {
        "update_id": 1,
        "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "Напомни завтра"},
    }
    assert client.post("/internal/updates", json=payload).status_code == 401
    headers = {"Authorization": "Bearer test"}
    assert client.post("/internal/updates", json=payload, headers=headers).status_code == 200
    assert client.post("/internal/updates", json=payload, headers=headers).json()["duplicate"]
    payload["update_id"] = 2
    payload["message"]["from"]["id"] = 99
    assert client.post("/internal/updates", json=payload, headers=headers).json()["ignored"]
    with sessions() as session:
        assert len(session.scalars(select(Update)).all()) == 1
        assert len(session.scalars(select(Job)).all()) == 1
        assert len(session.scalars(select(History)).all()) == 1
    assert (
        client.post("/internal/checkpoint", json={"value": 2}, headers=headers).status_code == 200
    )
    client.post("/internal/checkpoint", json={"value": 1}, headers=headers)
    assert client.get("/internal/checkpoint", headers=headers).json()["value"] == 2
    config.allowed_telegram_user_id = 99
    assert (
        client.get(
            "/internal/operations/"
            + client.post(
                "/internal/updates",
                json={
                    "update_id": 3,
                    "message": {"from": {"id": 99}, "chat": {"id": 99}, "text": "x"},
                },
                headers=headers,
            ).json()["operation_id"]
            + "/usage",
            headers=headers,
        ).status_code
        == 200
    )


def test_leases_expiry_retry_stale_and_owner_serialization(sessions):
    op_id = operation(sessions)
    with sessions.begin() as session:
        session.add_all(
            [
                Job(key=str(index), operation_id=op_id, kind="agent", payload={})
                for index in range(2)
            ]
        )
    with sessions.begin() as session:
        first = claim(session, Job, 42)
    with sessions.begin() as session:
        assert claim(session, Job, 42) is None
        renew(session, Job, first.id, first.lease_token)
    with sessions.begin() as session:
        row = session.get(Job, first.id)
        row.lease_until = now() - timedelta(seconds=1)
    with sessions.begin() as session:
        second = claim(session, Job, 42)
        assert second.id == first.id
        assert second.lease_token != first.lease_token
    with pytest.raises(LeaseLost), sessions.begin() as session:
        acknowledge(session, Job, first.id, first.lease_token)
    with sessions.begin() as session:
        acknowledge(session, Job, second.id, second.lease_token, "network")
        row = session.get(Job, second.id)
        assert row.status == "pending" and row.available_at > now()


def test_outbox_unicode_chunks_stable_and_persistent(sessions):
    op_id = operation(sessions)
    with sessions.begin() as session:
        op = session.get(Operation, op_id)
        enqueue_text(session, op, "😀" * 5000)
        enqueue_text(session, op, "😀" * 5000)
    with sessions() as session:
        rows = session.scalars(select(Outbox)).all()
        assert len(rows) == 3
        assert all(len(row.payload["text"].encode("utf-16-le")) // 2 <= 4000 for row in rows)

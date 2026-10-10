"""Conflict choice survives process restart; only an approval callback can delete."""

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app import api
from app.domain_actions import Approval
from app.domain_memory import MemoryEntry
from app.memory_resolution import resolution_state
from app.models import Invocation, MemoryContext, Operation, Outbox
from app.runtime import Runtime
from app.workers import deliver_once
from tests.test_memory_conflict_intent import ingress, owner_callback, registry, run, seed


class NoInterpretation:
    def estimate_request_budget(self, *args):
        raise AssertionError("Recorded resolution must not ask the model to reinterpret text")

    async def generate(self, *args):
        raise AssertionError("Recorded resolution must not ask the model to reinterpret text")


async def deliver(sessions):
    sent = []

    def telegram(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app),
            base_url="http://backend",
            headers={"Authorization": "Bearer test"},
        ) as backend,
        httpx.AsyncClient(
            transport=httpx.MockTransport(telegram), base_url="https://telegram/"
        ) as bot,
    ):
        for _ in range(20):
            with sessions() as session:
                pending = session.scalar(select(Outbox.id).where(Outbox.status == "pending"))
            if not pending:
                break
            await deliver_once(backend, bot)
        else:
            raise AssertionError("Outbox did not drain")
    return sent


async def conflict(sessions, monkeypatch, protocol):
    ids = await seed(sessions)
    query = ingress(sessions, monkeypatch, "Кто руководитель Сьерры?")
    await run(sessions, query, protocol, ("search_memory", {"query": "руководитель Сьерра"}))
    messages = await deliver(sessions)
    assert any("Лёша" in m.get("text", "") and "Саша" in m.get("text", "") for m in messages)
    return ids


async def choose(sessions, monkeypatch, protocol):
    op_id = ingress(sessions, monkeypatch, "Лёша — правильный вариант")
    provider = NoInterpretation()
    await Runtime(sessions, provider, registry(sessions, provider, api.settings()), protocol).run(
        op_id
    )
    with sessions() as session:
        card = session.scalar(select(Approval).where(Approval.operation_id == op_id))
        assert card and card.status == "pending"
        assert resolution_state(card)["state"] == "awaiting_approval"
        assert card.payload["choice_recorded"] == {
            "keep_entry_id": card.payload["retain_entry_id"],
            "delete_entry_id": card.payload["old_entry_id"],
        }
        assert session.get(Operation, op_id).status == "done"
        aid = card.id
    return aid


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_conflict_choice_approval_buttons_confirm_removes_conflict(
    sessions, monkeypatch, protocol
):
    ids = await conflict(sessions, monkeypatch, protocol)
    aid = await choose(sessions, monkeypatch, protocol)
    with sessions() as session:
        assert all(session.get(MemoryEntry, i) for i in ids)
        assert resolution_state(session.get(Approval, aid))["keep_entry_id"] == str(ids[0])
        assert resolution_state(session.get(Approval, aid))["delete_entry_id"] == str(ids[1])
    messages = await deliver(sessions)
    assert len(messages) == 1
    buttons = messages[0]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [f"a:{aid}:y", f"a:{aid}:n"]
    assert "Сохранить актуальную запись:\nЛёша" in messages[0]["text"]
    assert "Удалить старую запись:\nСаша" in messages[0]["text"]
    # Real ingress and leased callback; confirmation carries the displayed approval_id.
    assert uuid.UUID(buttons[0]["callback_data"].split(":")[1]) == aid
    await owner_callback(sessions, monkeypatch, aid)
    await owner_callback(sessions, monkeypatch, aid)
    await deliver(sessions)
    query = ingress(sessions, monkeypatch, "Кто теперь руководитель Сьерры?")
    await run(
        sessions,
        query,
        protocol,
        ("search_memory", {"query": "Сьерра"}),
        "Руководитель команды Сьерра — Лёша.",
    )
    with sessions() as session:
        assert resolution_state(session.get(Approval, aid))["state"] == "executed"
        assert session.get(MemoryEntry, ids[0]) and session.get(MemoryEntry, ids[2])
        assert not session.get(MemoryEntry, ids[1])
        result = session.scalar(select(Invocation).where(Invocation.operation_id == query)).result
        assert result["data"]["conflicts"] == [] and result["data"]["shown_pairs"] == []


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("confirm", ["да", "удалить", "a"])
async def test_choice_recorded_simple_confirm_never_reopens_version_question(
    sessions, monkeypatch, protocol, confirm
):
    ids = await conflict(sessions, monkeypatch, protocol)
    aid = await choose(sessions, monkeypatch, protocol)
    await deliver(sessions)
    # Truncate conversational history and introduce a later search without a conflict.
    later = ingress(sessions, monkeypatch, "Что я люблю пить?")
    await run(sessions, later, protocol, ("search_memory", {"query": "кофе"}))
    await deliver(sessions)
    with sessions.begin() as session:
        from app.models import History

        session.query(History).delete()
        context = session.scalar(select(MemoryContext).where(MemoryContext.operation_id == later))
        context.shown_conflicts = None
    # A fresh Runtime/provider instance simulates a process restart.
    for source in (confirm, "Лёша — правильный вариант"):
        op_id = ingress(sessions, monkeypatch, source)
        provider = NoInterpretation()
        await Runtime(
            sessions, provider, registry(sessions, provider, api.settings()), protocol
        ).run(op_id)
        messages = await deliver(sessions)
        assert len(messages) == 1
        assert messages[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"a:{aid}:y"
        assert "Уточните" not in messages[0]["text"] and "какую" not in messages[0]["text"]
    with sessions() as session:
        cards = session.scalars(select(Approval)).all()
        assert len(cards) == 1 and cards[0].id == aid
        assert resolution_state(cards[0])["state"] == "awaiting_approval"
        assert all(session.get(MemoryEntry, i) for i in ids)
    await owner_callback(sessions, monkeypatch, aid)
    await deliver(sessions)
    op_id = ingress(sessions, monkeypatch, confirm)
    provider = NoInterpretation()
    await Runtime(sessions, provider, registry(sessions, provider, api.settings()), protocol).run(
        op_id
    )
    assert "уже подтверждена" in (await deliver(sessions))[0]["text"]


@pytest.mark.parametrize("protocol", ["native", "json"])
async def test_expired_recorded_choice_never_reopens_or_deletes(sessions, monkeypatch, protocol):
    from datetime import timedelta

    from app.models import now

    ids = await conflict(sessions, monkeypatch, protocol)
    aid = await choose(sessions, monkeypatch, protocol)
    await deliver(sessions)
    with sessions.begin() as session:
        session.get(Approval, aid).expires_at = now() - timedelta(seconds=1)
    op_id = ingress(sessions, monkeypatch, "да")
    provider = NoInterpretation()
    await Runtime(sessions, provider, registry(sessions, provider, api.settings()), protocol).run(
        op_id
    )
    messages = await deliver(sessions)
    assert len(messages) == 1 and "Выбор версии сохранён" in messages[0]["text"]
    assert not messages[0].get("reply_markup")
    await owner_callback(sessions, monkeypatch, aid)
    with sessions() as session:
        assert resolution_state(session.get(Approval, aid))["state"] == "expired"
        assert all(session.get(MemoryEntry, i) for i in ids)


@pytest.mark.parametrize("boundary", ["owner", "chat"])
async def test_recorded_choice_not_reused_outside_owner_chat(sessions, monkeypatch, boundary):
    ids = await conflict(sessions, monkeypatch, "native")
    aid = await choose(sessions, monkeypatch, "native")
    await deliver(sessions)
    op_id = ingress(sessions, monkeypatch, "да", owner=99 if boundary == "owner" else 42, chat=99)
    requests = await run(sessions, op_id, final="Нет карточки для подтверждения в этом чате.")
    assert len(requests) == 1
    with sessions() as session:
        row = session.scalar(select(Outbox).where(Outbox.operation_id == op_id))
        assert not row.payload.get("reply_markup")
        assert "Лёша" not in row.payload["text"] and "Саша" not in row.payload["text"]
        assert session.get(Approval, aid).status == "pending"
        assert all(session.get(MemoryEntry, i) for i in ids)


async def test_replayed_model_choice_cannot_replace_recorded_pair(sessions, monkeypatch):
    from app.memory_resolution import ShownResolutionArgs, prepare_shown_resolution

    ids = await conflict(sessions, monkeypatch, "native")
    aid = await choose(sessions, monkeypatch, "native")
    await deliver(sessions)
    op_id = ingress(sessions, monkeypatch, "да")
    with sessions.begin() as session:
        row = session.get(Approval, aid)
        pair = row.payload["shown"]["pairs"][0]
        wrong_ref = next(k for k in ("a", "b") if pair[k] == str(ids[1]))
        inv = Invocation(
            operation_id=op_id,
            call_id="replayed-choice",
            name="prepare_memory_resolution",
            arguments={},
        )
        session.add(inv)
        session.flush()
        result = prepare_shown_resolution(
            session,
            session.get(Operation, op_id),
            inv.id,
            ShownResolutionArgs(conflict_ref=pair["conflict_ref"], retain_ref=wrong_ref),
        )
        assert result.data["approval_id"] == str(aid)
        assert result.data["keep_entry_id"] == str(ids[0])
        assert result.data["delete_entry_id"] == str(ids[1])
        assert len(session.scalars(select(Approval)).all()) == 1


async def test_confirm_revokes_pending_reissued_card_and_its_saved_copy(sessions, monkeypatch):
    from app.models import History, Update

    ids = await conflict(sessions, monkeypatch, "native")
    aid = await choose(sessions, monkeypatch, "native")
    await deliver(sessions)
    repeat = ingress(sessions, monkeypatch, "да")
    provider = NoInterpretation()
    await Runtime(sessions, provider, registry(sessions, provider, api.settings())).run(repeat)
    with sessions() as session:
        queued = session.scalar(select(Outbox).where(Outbox.operation_id == repeat))
        assert queued.status == "pending" and "Саша" in queued.payload["text"]
    await owner_callback(sessions, monkeypatch, aid)
    with sessions() as session:
        queued = session.scalar(select(Outbox).where(Outbox.operation_id == repeat))
        assert queued.status == "cancelled" and queued.payload == {}
        assert not session.scalar(select(History).where(History.operation_id == repeat))
        op = session.get(Operation, repeat)
        assert session.get(Update, op.update_id).payload == {}
        assert session.get(MemoryEntry, ids[0]) and session.get(MemoryEntry, ids[2])
        assert not session.get(MemoryEntry, ids[1])

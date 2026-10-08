"""Exercise measurement orchestration with the test HTTP adapter, never labelled live."""

import asyncio
import json

import httpx
from sqlalchemy import select

from app import api
from app.bootstrap import install
from app.config import Settings
from app.models import AICall, Checkpoint, Job, Operation, Update
from app.providers import YandexProvider
from app.workers import background_once
from scripts.benchmark import execute
from scripts.measurements import aggregate


async def test_all_four_measurement_scenarios_through_ingress_and_workers(
    sessions, tmp_path, monkeypatch
):
    config = Settings(
        ai_api_key="test",
        ai_folder_id="test",
        service_token="test",
        allowed_telegram_user_id=42,
        file_directory=tmp_path,
        ai_attempts=1,
    )
    monkeypatch.setattr(api, "settings", lambda: config)
    monkeypatch.setattr(api, "session_factory", lambda: sessions)

    def ai_transport(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "model": payload["model"],
                    "data": [{"embedding": [1.0] + [0.0] * 255}],
                    "usage": {"prompt_tokens": 10, "total_tokens": 10},
                },
            )
        messages = payload["messages"]
        last = messages[-1]
        if last["role"] == "tool":
            result = json.loads(last["content"])
            text = (
                json.dumps(
                    {
                        "has_evidence": True,
                        "answer": "500000 рублей",
                        "source_ids": [result["sources"][0]["source_id"]],
                    }
                )
                if result.get("sources")
                else "Готово: Иванов."
            )
            message = {"role": "assistant", "content": text}
        else:
            source = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            if source.startswith("Напомни") or source.startswith("Подготовь"):
                name = "create_task" if source.startswith("Напомни") else "prepare_meeting"
                clock = "15:00" if name == "create_task" else "16:00"
                args = {
                    "text": source,
                    "date": {
                        "kind": "absolute",
                        "source_phrase": f"завтра в {clock}",
                        "local_date": "2026-10-05",
                        "local_time": clock,
                    },
                }
            elif source.startswith("Запомни"):
                name, args = (
                    "save_memory",
                    {
                        "text": source,
                        "facts": [
                            {"entity": "проект", "predicate": "руководитель", "value": "Иванов"}
                        ],
                    },
                )
            elif "PDF benchmark-" in source:
                name, args = (
                    "search_document",
                    {"query": "бюджет", "document_id": source.split("ID ")[1].split("?")[0]},
                )
            else:
                name, args = "search_memory", {"query": source}
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [{"message": message}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            },
        )

    provider = YandexProvider(config, sessions, httpx.MockTransport(ai_transport))
    install(sessions, provider, config)
    manifest = {"run_id": "benchmark-test", "updates": {}, "operations": {}}

    async def process():
        while True:
            if not await background_once(sessions, provider, config):
                await asyncio.sleep(0.01)

    worker = asyncio.create_task(process())
    try:
        async with httpx.AsyncClient(
            base_url="http://backend",
            transport=httpx.ASGITransport(app=api.app),
            headers={"Authorization": "Bearer test"},
        ) as client:
            await asyncio.wait_for(
                execute(config, sessions, client, manifest, tmp_path / "manifest.json"), 30
            )
            # Same run is resumable/idempotent, with no new provider calls or operations.
            with sessions() as session:
                count = len(session.scalars(select(AICall)).all())
            await asyncio.wait_for(
                execute(config, sessions, client, manifest, tmp_path / "manifest.json"), 30
            )
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    with sessions() as session:
        rows = session.scalars(select(AICall)).all()
        assert len(rows) == count and count > 0
        assert all(row.cost_complete and row.latency_ms is not None for row in rows)
        assert {row.scenario for row in rows} == {
            "task_creation",
            "memory",
            "pdf_index",
            "pdf_question",
            "approval_preparation",
        }
        assert all(row.id < 0 for row in session.scalars(select(Update)))
        assert not session.get(Checkpoint, "telegram")
        assert all(op.status == "done" for op in session.scalars(select(Operation)))
        assert not session.scalar(select(Job.id).where(Job.kind == "document"))
    assert aggregate(rows)["costs"]["RUB"]["incomplete_calls"] == 0


async def test_local_upload_header_requires_auth_and_document_metadata(sessions, monkeypatch):
    config = Settings(service_token="test", allowed_telegram_user_id=42)
    monkeypatch.setattr(api, "settings", lambda: config)
    monkeypatch.setattr(api, "session_factory", lambda: sessions)
    payload = {"update_id": -99, "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "hi"}}
    async with httpx.AsyncClient(
        base_url="http://backend", transport=httpx.ASGITransport(app=api.app)
    ) as client:
        assert (
            await client.post("/internal/updates", json=payload, headers={"X-Local-Upload": "true"})
        ).status_code == 401
        response = await client.post(
            "/internal/updates",
            json=payload,
            headers={"X-Local-Upload": "true", "Authorization": "Bearer test"},
        )
        assert response.status_code == 422
    with sessions() as session:
        assert not session.get(Update, -99)


def test_live_pdf_amount_formatting_preserves_amount_and_source_validation():
    from scripts.benchmark import valid_fixture_answer

    for value in ("500000", "500 000", "500\u00a0000", "500\u202f000"):
        assert valid_fixture_answer(f"Бюджет {value} рублей. Источники: demo.pdf, стр. 1")
    for answer in (
        "Бюджет 550000 рублей. Источники: demo.pdf, стр. 1",
        "Бюджет 5000000 рублей. Источники: demo.pdf, стр. 1",
        "Бюджет 1 500 000 рублей. Источники: demo.pdf, стр. 1",
        "Бюджет 500 000 000 рублей. Источники: demo.pdf, стр. 1",
        "Бюджет 500000.50 рублей. Источники: demo.pdf, стр. 1",
        "Бюджет 550000 рублей.\n\nИсточники: report500000.pdf, стр. 1",
        "Бюджет 500000 рублей.",
        "В найденных фрагментах PDF недостаточно оснований для ответа.",
    ):
        assert not valid_fixture_answer(answer)

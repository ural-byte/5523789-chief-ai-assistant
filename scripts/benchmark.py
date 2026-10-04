"""Real four-scenario run through durable ingress and backend workers. No fake AI responses."""

import argparse
import asyncio
import io
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select

from app.config import settings
from app.db import session_factory
from app.domain_actions import Approval, Task
from app.domain_memory import Document, MemoryEntry
from app.models import AICall, History, Invocation, Operation, now
from app.runtime import grounded_answer
from scripts.measurements import aggregate, event_record, markdown


class BenchmarkError(Exception):
    pass


def pdf_fixture():
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 50 700 Td (Demo project budget is 500000 rubles.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


async def execute(config, sessions, client, manifest, manifest_path):
    if (
        manifest.setdefault("owner_id", config.allowed_telegram_user_id)
        != config.allowed_telegram_user_id
    ):
        raise BenchmarkError("manifest_owner_mismatch")
    run_id = manifest["run_id"]

    async def submit(name, text=None, document=False):
        # UUID-derived negative IDs never intersect Telegram's nonnegative update IDs;
        # benchmarking never calls or advances the polling checkpoint.
        update_id = manifest["updates"].setdefault(name, -1 - (uuid.uuid4().int % (2**62)))
        save(manifest_path, manifest)
        if name not in manifest["operations"]:
            message = {
                "from": {"id": config.allowed_telegram_user_id},
                "chat": {"id": config.allowed_telegram_user_id},
                "date": int(time.time()),
            }
            if document:
                message["document"] = {
                    "file_name": f"benchmark-{run_id}.pdf",
                    "file_id": "benchmark-local",
                }
            else:
                message["text"] = text
            response = await client.post(
                "/internal/updates",
                json={"update_id": update_id, "message": message},
                headers={"X-Local-Upload": "true"} if document else {},
            )
            response.raise_for_status()
            op_id = response.json().get("operation_id")
            if not op_id:
                with sessions() as session:
                    op_id = session.scalar(
                        select(Operation.id).where(Operation.update_id == update_id)
                    )
            if not op_id:
                raise BenchmarkError("ingress_operation_missing")
            manifest["operations"][name] = str(op_id)
            save(manifest_path, manifest)
        op_id = uuid.UUID(manifest["operations"][name])
        if document:
            # The benchmark's local fixture is uploaded through the same authenticated
            # file API. The synthetic download job is not dispatched to Telegram.
            response = await client.post(
                f"/internal/operations/{op_id}/file",
                files={"file": (f"benchmark-{run_id}.pdf", pdf_fixture(), "application/pdf")},
            )
            response.raise_for_status()
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            with sessions() as session:
                op = session.get(Operation, op_id)
                if op.status == "error":
                    raise BenchmarkError(f"{name}_failed")
                if op.status == "done":
                    return op_id
            await asyncio.sleep(1)
        raise BenchmarkError(f"{name}_timeout")

    task_id = await submit("task", f"Напомни завтра в 15:00: проверить демонстрацию {run_id}")
    with sessions() as session:
        if not session.scalar(select(Task.id).where(Task.operation_id == task_id)):
            raise BenchmarkError("task_not_created")
    memory_id = await submit("memory_save", f"Запомни: руководитель проекта {run_id} — Иванов.")
    with sessions() as session:
        entry_id = session.scalar(
            select(MemoryEntry.id).join(Invocation).where(Invocation.operation_id == memory_id)
        )
        if not entry_id:
            raise BenchmarkError("memory_not_saved")
    search_id = await submit(
        "memory_search", f"Кто руководитель проекта {run_id}? Найди в долговременной памяти."
    )
    with sessions() as session:
        found = session.scalar(
            select(Invocation).where(
                Invocation.operation_id == search_id, Invocation.name == "search_memory"
            )
        )
        if (
            not found
            or not found.result
            or str(entry_id)
            not in {item.get("id") for item in found.result.get("data", {}).get("memory", [])}
        ):
            raise BenchmarkError("memory_search_missing")
    index_id = await submit("pdf_index", document=True)
    with sessions() as session:
        doc = session.scalar(select(Document).where(Document.operation_id == index_id))
        if not doc or doc.status != "ready":
            raise BenchmarkError("pdf_not_indexed")
        doc_id = doc.id
    question_id = await submit(
        "pdf_question",
        f"Какой бюджет проекта в PDF benchmark-{run_id}.pdf, ID {doc_id}? Ответь с источником.",
    )
    with sessions() as session:
        invocation = session.scalar(
            select(Invocation).where(
                Invocation.operation_id == question_id, Invocation.name == "search_document"
            )
        )
        if (
            not invocation
            or not invocation.result
            or invocation.result.get("presentation") != "grounded"
        ):
            raise BenchmarkError("pdf_retrieval_missing")
        sources = invocation.result["sources"]
        if not sources or any(source["document_id"] != str(doc_id) for source in sources):
            raise BenchmarkError("pdf_wrong_document")
        final = session.scalar(
            select(History).where(History.operation_id == question_id).order_by(History.id.desc())
        )
        answer = grounded_answer(final.message.get("content", ""), sources) if final else ""
        if "500000" not in answer or "стр. 1" not in answer:
            raise BenchmarkError("pdf_grounded_answer_failed")
    approval_id = await submit(
        "approval", f"Подготовь встречу завтра в 16:00: демонстрация {run_id}"
    )
    with sessions() as session:
        action = session.scalar(select(Approval).where(Approval.operation_id == approval_id))
        if not action or action.status != "pending":
            raise BenchmarkError("approval_not_pending")


async def main(args):
    config = settings()
    if not (
        config.ai_api_key.get_secret_value()
        and config.ai_folder_id
        and config.service_token.get_secret_value()
        and config.allowed_telegram_user_id
    ):
        print(
            json.dumps(
                {"status": "pending", "reason": "AI/service/user environment is not configured"}
            )
        )
        return 2
    path = args.output or config.file_directory / "benchmarks" / str(uuid.uuid4())
    manifest_path = path / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {"run_id": str(uuid.uuid4()), "updates": {}, "operations": {}}
    )
    sessions = session_factory()
    status, error = "passed", None
    try:
        async with httpx.AsyncClient(
            base_url=config.backend_url,
            timeout=45,
            headers={"Authorization": "Bearer " + config.service_token.get_secret_value()},
        ) as client:
            await execute(config, sessions, client, manifest, manifest_path)
    except Exception as exc:
        status = "failed"
        error = str(exc) if isinstance(exc, BenchmarkError) else type(exc).__name__
    ids = [uuid.UUID(value) for value in manifest["operations"].values()]
    with sessions() as session:
        try:
            events = session.scalars(select(AICall).where(AICall.operation_id.in_(ids))).all()
        except Exception as exc:
            events = []
            status, error = "failed", type(exc).__name__
    group = {
        "Поручение": ["task"],
        "Долговременная память": ["memory_save", "memory_search"],
        "PDF: загрузка и индексация": ["pdf_index"],
        "PDF: вопрос": ["pdf_question"],
        "Подготовка approval": ["approval"],
    }
    report = {
        "status": status,
        "error_code": error,
        "measured_at": now().isoformat(),
        "revision": args.revision,
        "protocol": config.tool_protocol,
        "run_id": manifest["run_id"],
        "operations": manifest["operations"],
        "scenarios": {
            name: aggregate(
                e
                for e in events
                if str(e.operation_id) in {manifest["operations"].get(key) for key in keys}
            )
            for name, keys in group.items()
        },
        "total": aggregate(events),
        "events": [event_record(e) for e in events],
    }
    save(path / "report.json", report)
    (path / "report.md").write_text(markdown(report))
    print(
        json.dumps(
            {"status": status, "error_code": error, "report": str(path / "report.json")},
            ensure_ascii=False,
        )
    )
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Directory; reuse to resume the same run")
    parser.add_argument("--revision", default=os.environ.get("APP_REVISION", "unknown"))
    sys.exit(asyncio.run(main(parser.parse_args())))

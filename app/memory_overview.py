"""A paged original-record view; no generation or embedding calls."""

import time
import uuid

from sqlalchemy import func, select, tuple_

from app.domain_memory import Entity, Fact, MemoryEntry
from app.privacy import guard_operation
from app.queue import enqueue_text

PAGE_SIZE = 20


def overview_requested(source):
    from app.data_controls import normalize

    return normalize(source) in {
        "/memory",
        "что ты сейчас обо мне помнишь",
        "что ты обо мне помнишь",
    }


def truncate(value, limit=3500):
    result, units = [], 0
    for char in value:
        size = len(char.encode("utf-16-le")) // 2
        if units + size > limit - 40:
            return "".join(result) + "\n[Запись сокращена для показа]"
        result.append(char)
        units += size
    return value


def memory_page(session, op, cursor=None):
    started = time.monotonic()
    guard_operation(session, op.id)
    query = select(MemoryEntry).where(MemoryEntry.owner_id == op.owner_id)
    total = session.scalar(
        select(func.count()).select_from(MemoryEntry).where(MemoryEntry.owner_id == op.owner_id)
    )

    def measured_return(text, buttons):
        op.retrieval_ms = (op.retrieval_ms or 0) + round((time.monotonic() - started) * 1000)
        return text, buttons

    if cursor:
        anchor = session.get(MemoryEntry, cursor)
        if anchor is None or anchor.owner_id != op.owner_id:
            return measured_return(
                "Эта страница памяти больше недоступна. Откройте /memory заново.", []
            )
        query = query.where(
            tuple_(MemoryEntry.created_at, MemoryEntry.id) > (anchor.created_at, anchor.id)
        )
    entries = session.scalars(
        query.order_by(MemoryEntry.created_at, MemoryEntry.id).limit(PAGE_SIZE + 1)
    ).all()
    more = len(entries) > PAGE_SIZE
    entries = entries[:PAGE_SIZE]
    if not entries:
        return measured_return(
            "Сохранённых записей памяти пока нет. Попросите запомнить нужный факт.", []
        )
    # All fact versions are retained and displayed together with each original.
    lines = [f"Сохранённые записи: {total}. На этой странице: {len(entries)}."]
    fact_groups = {}
    for fact, entity in session.execute(
        select(Fact, Entity)
        .join(Entity)
        .where(Fact.entry_id.in_([e.id for e in entries]), Entity.owner_id == op.owner_id)
    ):
        fact_groups.setdefault(fact.entry_id, []).append((fact, entity))
    for entry in entries:
        facts = fact_groups.get(entry.id, [])
        text = entry.original
        if facts:
            text += "\nФакты (версии сохраняются отдельно):\n" + "\n".join(
                f"{entity.name} / {fact.predicate}: {fact.value}" for fact, entity in facts
            )
        lines.append(truncate(text))
    buttons = (
        [[{"text": "Следующие записи", "callback_data": f"m:{entries[-1].id}"}]] if more else []
    )
    return measured_return("\n\n".join(lines), buttons)


async def show_memory(sessions, job, lease, cursor=None):
    with sessions.begin() as session:
        op = guard_operation(session, job.operation_id, lease)
        text, buttons = memory_page(session, op, cursor)
        op.scenario, op.status = "memory_overview", "done"
        op.agent_loop_ms = 0
        enqueue_text(session, op, text, buttons)


def parse_cursor(value):
    if not isinstance(value, str) or not value.startswith("m:"):
        return None
    try:
        return uuid.UUID(value[2:])
    except ValueError:
        return None

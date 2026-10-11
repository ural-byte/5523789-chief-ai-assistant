from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, tuple_

from app.dates import DateSpec
from app.domain_actions import CreateTask, Task
from app.domain_memory import MemoryEntry, SaveMemory, SearchMemory
from app.memory_resolution import PrepareMemoryResolution
from app.privacy import guard_invocation
from app.tools import ToolResult


class TaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)
    date: DateSpec


class SpikeCreateTask(CreateTask):
    arguments = TaskArgs


class PageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, ge=1, le=25)
    after: UUID | None = None


class PageReader:
    arguments = PageArgs

    def __init__(self, model):
        self.model = model
        self.description = (
            "Показать сохранённые записи владельца с источниками."
            if model is MemoryEntry
            else "Показать активные поручения владельца и сроки."
        )

    async def prepare(self, ctx, arguments):
        return None

    def apply(self, session, ctx, args, prepared):
        guard_invocation(session, ctx)
        model = self.model
        query = select(model).where(model.owner_id == ctx.owner_id)
        order = model.created_at if model is MemoryEntry else model.deadline
        if model is Task:
            query = query.where(model.status == "pending")
        if args.after:
            cursor = session.get(model, args.after)
            if not cursor or cursor.owner_id != ctx.owner_id:
                raise ValueError("invalid_cursor")
            query = query.where(tuple_(order, model.id) < (getattr(cursor, order.key), cursor.id))
        rows = session.scalars(query.order_by(order.desc(), model.id.desc()).limit(args.limit + 1))
        rows = list(rows)
        more, page = len(rows) > args.limit, rows[: args.limit]
        items = [
            {
                "entry_id": str(row.id),
                "text": row.original,
                "source": row.source_text,
                "source_update_id": row.source_update_id,
                "created_at": row.created_at.isoformat(),
            }
            if model is MemoryEntry
            else {
                "task_id": str(row.id),
                "text": row.text,
                "deadline": row.deadline.isoformat(),
                "timezone": row.source_timezone,
            }
            for row in page
        ]
        return ToolResult(
            status="ok",
            data={"items": items, "next_cursor": str(page[-1].id) if more else None},
        )


def capabilities(sessions, embeddings):
    return {
        "save_memory": SaveMemory(sessions, embeddings),
        "search_memory": SearchMemory(sessions, embeddings),
        "list_memory": PageReader(MemoryEntry),
        "delete_memory": PrepareMemoryResolution(),
        "create_task": SpikeCreateTask(),
        "list_tasks": PageReader(Task),
    }

import asyncio
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, event, inspect, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.dates import DateSpec, resolve_date
from app.db import Base
from app.models import Invocation, Operation, Outbox, now
from app.queue import enqueue_text
from app.tools import ToolResult


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    invocation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Invocation.id), unique=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Operation.id))
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(String)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source_timezone: Mapped[str] = mapped_column(String)
    reference_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, default="pending")


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    invocation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Invocation.id), unique=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Operation.id))
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSONB)
    action_kind: Mapped[str] = mapped_column(String, default="meeting")
    stale_reason: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)
    date: DateSpec
    complete_request: bool = Field(
        default=False,
        description="True only if this action completes every part of the user's request.",
    )


def source_text(ctx):
    return ctx.source_update.get("message", {}).get("text", "")


def display_time(value, timezone):
    return value.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M") + f" ({timezone})"


class CreateTask:
    arguments = ActionArgs
    description = (
        "Создать напоминание. Точную дату извлеки из сообщения; source_phrase — его подстрока."
    )

    async def prepare(self, ctx, arguments):
        return resolve_date(arguments.date, ctx.reference_at, ctx.timezone, source_text(ctx))

    def apply(self, session, ctx, arguments, prepared):
        from app.privacy import guard_operation

        guard_operation(session, ctx.operation_id, ctx.lease, context=True)
        deadline, question = prepared
        if question:
            return ToolResult(
                status="needs_clarification", user_message=question, presentation="canonical"
            )
        row = session.scalar(
            select(Task).where(
                Task.operation_id == ctx.operation_id,
                Task.text == arguments.text,
                Task.deadline == deadline,
            )
        )
        if row is None:
            row = Task(
                invocation_id=uuid.UUID(ctx.idempotency_key),
                operation_id=ctx.operation_id,
                owner_id=ctx.owner_id,
                chat_id=ctx.chat_id,
                text=arguments.text,
                deadline=deadline,
                source_timezone=ctx.timezone,
                reference_at=ctx.reference_at,
            )
            session.add(row)
            session.flush()
        return ToolResult(
            status="ok",
            data={"task_id": str(row.id)},
            presentation="canonical",
            user_message=(
                f"Поручение создано: {row.text}\n"
                f"Напомню {display_time(row.deadline, row.source_timezone)}."
            ),
        )


class PrepareMeeting(CreateTask):
    description = (
        "Подготовить simulated-встречу с обязательным подтверждением; source_phrase из сообщения."
    )

    def apply(self, session, ctx, arguments, prepared):
        from app.privacy import guard_operation

        guard_operation(session, ctx.operation_id, ctx.lease, context=True)
        deadline, question = prepared
        if question:
            return ToolResult(
                status="needs_clarification", user_message=question, presentation="canonical"
            )
        row = session.scalar(
            select(Approval).where(
                Approval.operation_id == ctx.operation_id,
                Approval.action_kind == "meeting",
                Approval.payload["title"].astext == arguments.text,
                Approval.payload["at"].astext == deadline.isoformat(),
            )
        )
        if row is None:
            row = Approval(
                invocation_id=uuid.UUID(ctx.idempotency_key),
                operation_id=ctx.operation_id,
                owner_id=ctx.owner_id,
                chat_id=ctx.chat_id,
                payload={
                    "title": arguments.text,
                    "at": deadline.isoformat(),
                    "timezone": ctx.timezone,
                    "mode": "simulated",
                },
                expires_at=now() + timedelta(hours=24),
            )
            session.add(row)
            session.flush()
        canonical_time = display_time(
            datetime.fromisoformat(row.payload["at"]), row.payload["timezone"]
        )
        return ToolResult(
            status="ok",
            data={"approval_id": str(row.id), "payload": row.payload},
            presentation="canonical",
            user_message=(
                f"Подтвердите встречу: {row.payload['title']}\n"
                f"{canonical_time}\n"
                "Режим: симуляция. Реальный календарь не изменяется."
            ),
            buttons=[
                [
                    {"text": "Подтвердить", "callback_data": f"a:{row.id}:y"},
                    {"text": "Отменить", "callback_data": f"a:{row.id}:n"},
                ]
            ],
        )


def scheduler_once(sessions, owner_id, timestamp=None):
    timestamp = timestamp or now()
    with sessions.begin() as session:
        from app.privacy import owner_lock

        owner_lock(session, owner_id)
        rows = session.scalars(
            select(Task)
            .where(Task.owner_id == owner_id, Task.status == "pending", Task.deadline <= timestamp)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            delay = max(0, int((timestamp - row.deadline).total_seconds()))
            text = f"Напоминание: {row.text}"
            if delay > 10:
                text += f"\nОтправлено с задержкой {delay} сек. после срока."
            key = f"task:{row.id}:reminder"
            if not session.scalar(select(Outbox.id).where(Outbox.key == key)):
                session.add(
                    Outbox(
                        key=key,
                        operation_id=row.operation_id,
                        kind="sendMessage",
                        payload={"chat_id": row.chat_id, "text": text},
                        purpose="notification",
                    )
                )
            row.status = "notified"
        for row in session.scalars(
            select(Approval)
            .where(
                Approval.owner_id == owner_id,
                Approval.status == "pending",
                Approval.expires_at <= timestamp,
            )
            .with_for_update(skip_locked=True)
        ):
            row.status = "expired"


async def scheduler_loop(sessions, owner_id):
    while True:
        try:
            await asyncio.to_thread(scheduler_once, sessions, owner_id)
        except Exception as exc:
            import logging

            logging.getLogger("scheduler").error("scheduler_failed code=%s", type(exc).__name__)
        await asyncio.sleep(5)


async def handle_callback(sessions, job, lease):
    query = job.payload["callback_query"]
    with sessions.begin() as session:
        from app.privacy import guard_operation

        op = guard_operation(session, job.operation_id, lease)
        from app.memory_overview import memory_page, parse_cursor

        cursor = parse_cursor(query.get("data"))
        if cursor:
            message, buttons = memory_page(session, op, cursor)
            enqueue_text(session, op, message, buttons)
            op.scenario, op.status = "memory_overview", "done"
            return
        message = "Подтверждение недоступно."
        try:
            prefix, raw_id, choice = query.get("data", "").split(":")
            approval_id = uuid.UUID(raw_id)
            if prefix != "a" or choice not in {"y", "n"}:
                raise ValueError("callback")
        except (ValueError, AttributeError):
            approval_id, choice = None, None
        row = (
            session.scalar(
                select(Approval)
                .where(
                    Approval.id == approval_id,
                    Approval.owner_id == op.owner_id,
                    Approval.chat_id == op.chat_id,
                )
                .with_for_update()
            )
            if approval_id
            else None
        )
        if row and row.action_kind not in {
            "meeting",
            "data_deletion",
            "memory_resolution",
            "memory_resolution_shown",
        }:
            row = None
        if row and row.action_kind in {"memory_resolution", "memory_resolution_shown"}:
            if type(row.payload.get("schema")) is not int or (
                row.action_kind,
                row.payload.get("schema"),
            ) not in {
                ("memory_resolution", 1),
                ("memory_resolution_shown", 2),
            }:
                row = None
        if row:
            if row.status == "pending" and row.expires_at <= now():
                row.status = "expired"
            if row.status == "pending":
                if choice == "n":
                    row.status = "cancelled"
                    message = (
                        "Действие отменено."
                        if row.action_kind != "meeting"
                        else "Встреча отменена."
                    )
                elif row.action_kind in {"memory_resolution", "memory_resolution_shown"}:
                    from app.memory_resolution import execute_resolution

                    message = (
                        "Старая запись и связанные копии удалены. Актуальная версия сохранена."
                        if execute_resolution(session, row)
                        else "Данные изменились. Подготовьте действие заново."
                    )
                elif row.action_kind == "data_deletion":
                    from app.data_controls import fence_delete

                    fence_delete(session, row)
                    message = "Начал удаление. Сообщу, когда оно завершится."
                else:
                    row.status = "approved"
                    row.approved_at = now()
                    session.flush()
                    # The simulated executor mutates no external system. Both transitions
                    # commit atomically; an interrupted transaction leaves the action pending.
                    row.status = "simulated"
                    row.executed_at = now()
                    message = f"Встреча выполнена в режиме симуляции: {row.payload['title']}."
            else:
                message = {
                    "simulated": "Встреча уже выполнена в режиме симуляции.",
                    "cancelled": (
                        "Действие уже отменено."
                        if row.action_kind != "meeting"
                        else "Встреча уже отменена."
                    ),
                    "expired": "Срок подтверждения истёк.",
                    "stale": "Данные изменились. Подготовьте действие заново.",
                    "executing": "Удаление продолжается. Сообщу, когда оно завершится.",
                    "executed": "Данные уже удалены. Повторное удаление не выполняется.",
                }.get(row.status, message)
        from app.memory_output import validate_memory_output
        from app.models import ApprovalAudit

        validate_memory_output(message)

        if not session.get(ApprovalAudit, str(query["id"])):
            session.add(
                ApprovalAudit(
                    callback_id=str(query["id"]),
                    approval_id=row.id if row else None,
                    operation_id=op.id,
                    outcome=row.status if row else "unavailable",
                )
            )
        enqueue_text(session, op, message)
        key = f"callback:{query['id']}:answer"
        if not session.scalar(select(Outbox.id).where(Outbox.key == key)):
            session.add(
                Outbox(
                    key=key,
                    operation_id=op.id,
                    kind="answerCallbackQuery",
                    payload={"callback_query_id": query["id"], "text": message[:180]},
                    purpose="callback",
                )
            )
        op.status = "done"


@event.listens_for(Approval, "before_update")
def immutable_approval_payload(mapper, connection, target):
    if any(
        inspect(target).attrs[key].history.has_changes()
        for key in (
            "payload",
            "owner_id",
            "chat_id",
            "action_kind",
            "operation_id",
            "invocation_id",
        )
    ):
        raise ValueError("approval_payload_immutable")

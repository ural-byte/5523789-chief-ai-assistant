import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def now():
    return datetime.now(UTC)


class Update(Base):
    __tablename__ = "telegram_updates"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    update_id: Mapped[int | None] = mapped_column(ForeignKey(Update.id), unique=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operations.id"))
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    scenario: Mapped[str] = mapped_column(String, default="conversation")
    status: Mapped[str] = mapped_column(String, default="pending")
    input_spent: Mapped[int] = mapped_column(Integer, default=0)
    tool_steps: Mapped[int] = mapped_column(Integer, default=0)
    reference_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    timezone: Mapped[str] = mapped_column(String)
    context_epoch: Mapped[int] = mapped_column(Integer, default=0)
    source_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=now)
    first_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_delivery_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieval_ms: Mapped[int | None] = mapped_column(Integer)
    agent_loop_ms: Mapped[int | None] = mapped_column(Integer)
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: now() + timedelta(seconds=90)
    )
    terminal_revision: Mapped[int] = mapped_column(Integer, default=0)
    delivery_state: Mapped[str] = mapped_column(String, default="pending")
    error_reason: Mapped[str | None] = mapped_column(String)
    recovery_created: Mapped[bool] = mapped_column(default=False)


class UserState(Base):
    __tablename__ = "user_states"
    owner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    context_epoch: Mapped[int] = mapped_column(Integer, default=0)


class Tombstone(Base):
    __tablename__ = "tombstones"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    approval_id: Mapped[uuid.UUID] = mapped_column(UUID)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MemoryContext(Base):
    __tablename__ = "memory_contexts"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Operation.id), index=True)
    invocation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invocations.id"), unique=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    context_epoch: Mapped[int] = mapped_column(Integer)
    entries: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ApprovalPreview(Base):
    __tablename__ = "approval_previews"
    approval_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(String)


class FileIntent(Base):
    __tablename__ = "file_intents"
    path: Mapped[str] = mapped_column(String, primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Operation.id), index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)


class DeletionCleanup(Base):
    __tablename__ = "deletion_cleanups"
    approval_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    paths: Mapped[list] = mapped_column(JSONB)
    remaining: Mapped[list] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String)


class ApprovalAudit(Base):
    __tablename__ = "approval_audit"
    callback_id: Mapped[str] = mapped_column(String, primary_key=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Operation.id))
    outcome: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Activity(Base):
    __tablename__ = "activities"
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Operation.id), primary_key=True)
    next_typing_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    progress_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    progress_sent: Mapped[bool] = mapped_column(default=False)
    document: Mapped[bool] = mapped_column(default=False)


class History(Base):
    __tablename__ = "history"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Operation.id), index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message: Mapped[dict] = mapped_column(JSONB)


class Invocation(Base):
    __tablename__ = "invocations"
    __table_args__ = (UniqueConstraint("operation_id", "call_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Operation.id))
    call_id: Mapped[str] = mapped_column(String)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String)
    arguments: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    model_result: Mapped[dict | None] = mapped_column(JSONB)


class QueueItem(Base):
    __abstract__ = True
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String, unique=True)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(Operation.id))
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID)
    error_code: Mapped[str | None] = mapped_column(String)


class Job(QueueItem):
    __tablename__ = "jobs"


class Outbox(QueueItem):
    __tablename__ = "outbox"
    purpose: Mapped[str] = mapped_column(String, default="final")
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    send_latency_ms: Mapped[int | None] = mapped_column(Integer)
    delivery_deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: now() + timedelta(seconds=60)
    )
    terminal_revision: Mapped[int] = mapped_column(Integer, default=0)
    failure_class: Mapped[str | None] = mapped_column(String)
    terminal_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AICall(Base):
    __tablename__ = "ai_calls"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Operation.id), index=True)
    logical_call_id: Mapped[uuid.UUID] = mapped_column(UUID)
    attempt: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    operation_type: Mapped[str] = mapped_column(String)
    scenario: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="started")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    tool_tokens: Mapped[int | None] = mapped_column(Integer)
    embedding_tokens: Mapped[int | None] = mapped_column(Integer)
    extra_usage: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    cost_complete: Mapped[bool] = mapped_column(default=False)
    currency: Mapped[str] = mapped_column(String)
    pricing_version: Mapped[str] = mapped_column(String)
    pricing_snapshot: Mapped[dict] = mapped_column(JSONB)


class Heartbeat(Base):
    __tablename__ = "heartbeats"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Checkpoint(Base):
    __tablename__ = "checkpoints"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0)

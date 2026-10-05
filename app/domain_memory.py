"""Persistent structured facts, semantic memory and restartable text-PDF indexing."""

import asyncio
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from pgvector.sqlalchemy import Vector
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.file_store import ManagedFiles, guarded_open, register_writer
from app.models import Invocation, Job, Operation, Update, now
from app.privacy import guard_invocation, guard_operation
from app.queue import enqueue_text
from app.tools import ToolResult

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 100


class MemoryEntry(Base):
    __tablename__ = "memory_entries"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    invocation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Invocation.id), unique=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    original: Mapped[str] = mapped_column(String)
    source_text: Mapped[str] = mapped_column(String)
    source_update_id: Mapped[int | None] = mapped_column(BigInteger)
    embedding: Mapped[list] = mapped_column(Vector(256))
    embedding_model: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String)


class Fact(Base):
    __tablename__ = "facts"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(MemoryEntry.id))
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Entity.id))
    predicate: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(String)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Operation.id), unique=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String)
    file_path: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    pages: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="uploaded")
    error_code: Mapped[str | None] = mapped_column(String)


class Chunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "page", "ordinal"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(Document.id), index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    page: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(String)
    embedding: Mapped[list] = mapped_column(Vector(256))
    embedding_model: Mapped[str] = mapped_column(String)


class FactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1000)


class SaveArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    facts: list[FactInput] = Field(default_factory=list, max_length=20)


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000)
    entity: str | None = Field(default=None, max_length=200)


class DocumentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000)
    document_id: uuid.UUID | None = None
    document_name: str | None = Field(default=None, max_length=200)


def explicitly_requested(source):
    # A mention/quotation is data, not a command. Restrict writes to clear original
    # imperatives; the model cannot grant permission by paraphrasing a question.
    return bool(
        re.match(
            r"^\s*(?:пожалуйста\s*[,.:]?\s*|прошу\s+)?"
            r"(?:запомни(?:те)?|(?:запомнить)(?=\s*[:—-])|"
            r"сохрани(?:те)?\s+(?:этот\s+)?(?:факт|информацию|в\s+память))"
            r"(?:\s*[:—-]\s*|\s+)\S",
            source,
            re.I,
        )
    )


class SaveMemory:
    arguments = SaveArgs
    description = (
        "Сохранить память только по явной просьбе запомнить; facts отдельно от исходного текста."
    )

    def __init__(self, sessions, provider):
        self.sessions, self.provider = sessions, provider

    async def prepare(self, ctx, args):
        with self.sessions.begin() as session:
            op = guard_invocation(session, ctx)
            if session.scalar(
                select(MemoryEntry.id).where(
                    MemoryEntry.invocation_id == uuid.UUID(ctx.idempotency_key)
                )
            ):
                return "existing"
            update = session.get(Update, op.update_id) if op.update_id is not None else None
            source = (
                update.payload.get("message", {}).get("text", "")
                if update
                else ctx.source_update.get("message", {}).get("text", "")
            )
            if not explicitly_requested(source):
                return None
        return await self.provider.embed(ctx.operation_id, args.text, "doc", ctx.lease)

    def apply(self, session, ctx, args, prepared):
        op = guard_invocation(session, ctx)
        if prepared is None:
            return ToolResult(
                status="needs_clarification",
                presentation="canonical",
                user_message="Для сохранения факта явно попросите запомнить его.",
            )
        entry = session.scalar(
            select(MemoryEntry)
            .join(Invocation)
            .where(Invocation.operation_id == ctx.operation_id, MemoryEntry.original == args.text)
        )
        if entry is None:
            update = session.get(Update, op.update_id) if op.update_id is not None else None
            source = (
                update.payload.get("message", {}).get("text", "")
                if update
                else ctx.source_update.get("message", {}).get("text", "")
            )
            if not explicitly_requested(source):
                return ToolResult(
                    status="needs_clarification",
                    presentation="canonical",
                    user_message="Для сохранения факта явно попросите запомнить его.",
                )
            entry = MemoryEntry(
                invocation_id=uuid.UUID(ctx.idempotency_key),
                owner_id=ctx.owner_id,
                original=args.text,
                source_text=source,
                source_update_id=op.update_id
                if op.update_id is not None
                else ctx.source_update.get("update_id"),
                embedding=prepared.vector,
                embedding_model=prepared.model,
            )
            session.add(entry)
            session.flush()
            for item in args.facts:
                name = item.entity.strip().casefold()
                entity = session.scalar(
                    select(Entity).where(Entity.owner_id == ctx.owner_id, Entity.name == name)
                )
                if entity is None:
                    entity = Entity(owner_id=ctx.owner_id, name=name)
                    session.add(entity)
                    session.flush()
                session.add(
                    Fact(
                        entry_id=entry.id,
                        entity_id=entity.id,
                        predicate=item.predicate.strip().casefold(),
                        value=item.value,
                    )
                )
        return ToolResult(
            status="ok",
            presentation="canonical",
            data={"entry_id": str(entry.id)},
            user_message="Сохранил в долговременную память: " + entry.original,
        )


class SearchMemory:
    arguments = SearchArgs
    description = "Найти ранее сохранённые факты и семантическую память; противоречия не скрывать."

    def __init__(self, sessions, provider):
        self.sessions, self.provider = sessions, provider

    async def prepare(self, ctx, args):
        query = await self.provider.embed(ctx.operation_id, args.query, "query", ctx.lease)
        started = time.monotonic()
        with self.sessions() as session:
            entries = session.scalars(
                select(MemoryEntry)
                .where(MemoryEntry.owner_id == ctx.owner_id)
                .order_by(MemoryEntry.embedding.cosine_distance(query.vector))
                .limit(5)
            ).all()
            facts_query = (
                select(Fact, Entity, MemoryEntry)
                .join(Entity, Fact.entity_id == Entity.id)
                .join(MemoryEntry, Fact.entry_id == MemoryEntry.id)
                .where(Entity.owner_id == ctx.owner_id)
            )
            if args.entity:
                facts_query = facts_query.where(Entity.name == args.entity.strip().casefold())
            else:
                facts_query = facts_query.where(MemoryEntry.id.in_([entry.id for entry in entries]))
            facts = session.execute(
                facts_query.order_by(MemoryEntry.created_at, Fact.id).limit(50)
            ).all()
            groups = {(fact.entity_id, fact.predicate) for fact, _, _ in facts}
            if groups:
                facts = session.execute(
                    select(Fact, Entity, MemoryEntry)
                    .join(Entity, Fact.entity_id == Entity.id)
                    .join(MemoryEntry, Fact.entry_id == MemoryEntry.id)
                    .where(
                        Entity.owner_id == ctx.owner_id,
                        tuple_(Fact.entity_id, Fact.predicate).in_(groups),
                    )
                    .order_by(MemoryEntry.created_at, Fact.id)
                ).all()
        record_retrieval(self.sessions, ctx, started)
        return {
            "memory": [
                {"id": str(e.id), "text": e.original, "source": e.source_text} for e in entries
            ],
            "facts": [
                {
                    "fact_id": str(f.id),
                    "entity": ent.name,
                    "predicate": f.predicate,
                    "value": f.value,
                    "source": entry.source_text,
                    "entry_id": str(entry.id),
                }
                for f, ent, entry in facts
            ],
        }

    def apply(self, session, ctx, args, prepared):
        guard_operation(session, ctx.operation_id, ctx.lease, context=True)
        original_ids = {uuid.UUID(e["id"]) for e in prepared["memory"]}
        available = set(
            session.scalars(
                select(MemoryEntry.id).where(
                    MemoryEntry.owner_id == ctx.owner_id, MemoryEntry.id.in_(original_ids)
                )
            )
        )
        if available != original_ids or any(
            uuid.UUID(f["entry_id"])
            not in set(
                session.scalars(select(MemoryEntry.id).where(MemoryEntry.owner_id == ctx.owner_id))
            )
            for f in prepared["facts"]
        ):
            from app.privacy import ContextChanged

            raise ContextChanged()
        if not prepared["memory"] and not prepared["facts"]:
            return ToolResult(
                status="ok",
                presentation="canonical",
                user_message="Сохранённых сведений по этому запросу нет.",
            )
        from app.memory_resolution import remember_context

        op = session.get(Operation, ctx.operation_id)
        context = remember_context(
            session,
            op,
            uuid.UUID(ctx.idempotency_key),
            original_ids | {uuid.UUID(f["entry_id"]) for f in prepared["facts"]},
        )
        prepared["context_id"] = str(context.id)
        groups = {}
        for fact in prepared["facts"]:
            groups.setdefault((fact["entity"], fact["predicate"]), []).append(fact)
        prepared["conflicts"] = [
            versions
            for versions in groups.values()
            if len({fact["value"].strip().casefold() for fact in versions}) > 1
        ]
        return ToolResult(status="ok", data=prepared)


def extract_chunks(path):
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("encrypted_pdf")
    if len(reader.pages) > MAX_PAGES:
        raise ValueError("pdf_page_limit")
    result = []
    total_chars = 0
    for page_index, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        total_chars += len(text)
        if total_chars > 2_000_000:
            raise ValueError("pdf_text_limit")
        for ordinal, start in enumerate(range(0, len(text), 1800)):
            result.append((page_index, ordinal, text[start : start + 2000]))
            if start + 2000 >= len(text):
                break
    if not result:
        raise ValueError("pdf_no_text")
    return len(reader.pages), result


class Documents:
    def __init__(self, sessions, provider, config):
        self.sessions, self.provider, self.config = sessions, provider, config

    async def upload(self, op, name, media, stream, lease=None):
        with self.sessions() as session:
            old = session.scalar(select(Document).where(Document.operation_id == op.id))
            if old:
                return {"document_id": str(old.id), "duplicate": True}
        if not name.casefold().endswith(".pdf"):
            raise HTTPException(422, "Only text PDF is supported")
        temporary, final = register_writer(self.sessions, op.id, lease)
        size, prefix = 0, b""
        with ManagedFiles(self.config.file_directory) as files:
            try:
                with guarded_open(self.sessions, files, op.id, temporary, lease) as handle:
                    while data := await stream.read(64 * 1024):
                        size += len(data)
                        if size > MAX_BYTES:
                            raise HTTPException(413, "PDF exceeds 10 MiB")
                        if not prefix:
                            prefix = data[:5]
                        handle.write(data)
                if prefix != b"%PDF-":
                    raise HTTPException(422, "Invalid PDF header")
                with self.sessions.begin() as session:
                    locked = guard_operation(session, op.id, lease)
                    if locked.owner_id != self.config.allowed_telegram_user_id:
                        raise HTTPException(404, "Operation not found")
                    old = session.scalar(select(Document).where(Document.operation_id == op.id))
                    if old:
                        return {"document_id": str(old.id), "duplicate": True}
                    files.replace(temporary, final)
                    document = Document(
                        operation_id=op.id,
                        owner_id=op.owner_id,
                        name=Path(name).name[:200],
                        file_path=str(self.config.file_directory / final),
                        size=size,
                    )
                    session.add(document)
                    session.flush()
                    locked.scenario = "pdf_index"
                    session.add(
                        Job(
                            key=f"document:{document.id}:index",
                            operation_id=op.id,
                            kind="pdf_index",
                            payload={"document_id": str(document.id)},
                        )
                    )
                    return {"document_id": str(document.id)}
            finally:
                files.unlink(temporary)

    async def index(self, job, lease):
        document_id = uuid.UUID(job.payload["document_id"])
        with self.sessions() as session:
            document = session.get(Document, document_id)
            if document is None:
                from app.queue import LeaseLost

                raise LeaseLost("document removed")
            if document.status in {"ready", "failed"}:
                return
        try:

            def extract_managed():
                with ManagedFiles(self.config.file_directory) as files:
                    with self.sessions.begin() as session:
                        guard_operation(session, job.operation_id, lease)
                        handle = files.open_reader(f"{document.operation_id}.pdf")
                    with handle:
                        return extract_chunks(handle)

            pages, texts = await asyncio.to_thread(extract_managed)
            with self.sessions.begin() as session:
                guard_operation(session, job.operation_id, lease)
                row = session.get(Document, document_id)
                row.status = "indexing"
                row.pages = pages
            for page, ordinal, text in texts:
                with self.sessions() as session:
                    existing = session.scalar(
                        select(Chunk.id).where(
                            Chunk.document_id == document_id,
                            Chunk.page == page,
                            Chunk.ordinal == ordinal,
                        )
                    )
                if existing:
                    continue
                embedding = await self.provider.embed(job.operation_id, text, "doc", lease)
                with self.sessions.begin() as session:
                    guard_operation(session, job.operation_id, lease)
                    if not session.scalar(
                        select(Chunk.id).where(
                            Chunk.document_id == document_id,
                            Chunk.page == page,
                            Chunk.ordinal == ordinal,
                        )
                    ):
                        session.add(
                            Chunk(
                                document_id=document_id,
                                owner_id=document.owner_id,
                                page=page,
                                ordinal=ordinal,
                                text=text,
                                embedding=embedding.vector,
                                embedding_model=embedding.model,
                            )
                        )
            with self.sessions.begin() as session:
                guard_operation(session, job.operation_id, lease)
                session.get(Document, document_id).status = "ready"
                op = session.get(Operation, job.operation_id)
                op.status = "done"
                enqueue_text(
                    session,
                    op,
                    f"Документ «{document.name}» обработан, страниц: {pages}. "
                    "Можно задавать вопросы.",
                )
        except Exception as exc:
            from app.queue import LeaseLost

            if isinstance(exc, LeaseLost):
                raise
            code = (
                str(exc)
                if isinstance(exc, ValueError) and str(exc).startswith("pdf_")
                else type(exc).__name__
            )
            with self.sessions.begin() as session:
                guard_operation(session, job.operation_id, lease)
                row = session.get(Document, document_id)
                row.status = "failed"
                row.error_code = code
                op = session.get(Operation, job.operation_id)
                op.status = "error"
                message = "Не удалось обработать PDF. Проверьте текстовый слой и файл."
                if code == "pdf_no_text":
                    message = "В PDF нет текстового слоя. OCR не поддерживается."
                elif code == "pdf_page_limit":
                    message = "PDF содержит больше 100 страниц."
                enqueue_text(session, op, message)


class SearchDocument:
    arguments = DocumentArgs
    description = (
        "Поиск по PDF пользователя. Передай имя или ID; "
        "при нескольких документах сначала уточни выбор."
    )

    def __init__(self, sessions, provider):
        self.sessions, self.provider = sessions, provider

    async def prepare(self, ctx, args):
        started = time.monotonic()
        with self.sessions() as session:
            statement = select(Document).where(Document.owner_id == ctx.owner_id)
            if args.document_id:
                statement = statement.where(Document.id == args.document_id)
            elif args.document_name:
                statement = statement.where(Document.name == args.document_name)
            docs = session.scalars(statement).all()
        record_retrieval(self.sessions, ctx, started)
        if len(docs) != 1:
            return ToolResult(
                status="needs_clarification",
                presentation="canonical",
                user_message=(
                    "Укажите PDF по имени или ID:\n" + "\n".join(f"{d.name} — {d.id}" for d in docs)
                )
                if docs
                else "Загрузите текстовый PDF.",
                data={
                    "documents": [
                        {"id": str(d.id), "name": d.name, "status": d.status} for d in docs
                    ]
                },
            )
        document = docs[0]
        if document.status != "ready":
            return ToolResult(
                status="error",
                presentation="canonical",
                user_message="Документ пока не готов для вопросов или его не удалось обработать.",
            )
        with self.sessions.begin() as session:
            guard_operation(session, ctx.operation_id, ctx.lease, context=True)
            session.get(Operation, ctx.operation_id).parent_id = document.operation_id
        vector = await self.provider.embed(ctx.operation_id, args.query, "query", ctx.lease)
        started = time.monotonic()
        with self.sessions() as session:
            chunks = session.scalars(
                select(Chunk)
                .where(Chunk.owner_id == ctx.owner_id, Chunk.document_id == document.id)
                .order_by(Chunk.embedding.cosine_distance(vector.vector))
                .limit(5)
            ).all()
        record_retrieval(self.sessions, ctx, started)
        if not chunks:
            return ToolResult(
                status="ok",
                presentation="canonical",
                user_message="В PDF не найдено оснований для ответа.",
            )
        return ToolResult(
            status="ok",
            presentation="grounded",
            sources=[
                {
                    "source_id": str(c.id),
                    "chunk_id": str(c.id),
                    "document_id": str(document.id),
                    "document_name": document.name,
                    "page": c.page,
                    "excerpt": c.text,
                }
                for c in chunks
            ],
        )

    def apply(self, session, ctx, args, prepared):
        guard_operation(session, ctx.operation_id, ctx.lease, context=True)
        if prepared.sources:
            found = set(
                session.scalars(
                    select(Chunk.id).where(
                        Chunk.owner_id == ctx.owner_id,
                        Chunk.id.in_([uuid.UUID(s["chunk_id"]) for s in prepared.sources]),
                    )
                )
            )
            if found != {uuid.UUID(s["chunk_id"]) for s in prepared.sources}:
                from app.privacy import ContextChanged

                raise ContextChanged()
        return prepared


def record_retrieval(sessions, ctx, started):
    duration = round((time.monotonic() - started) * 1000)
    with sessions.begin() as session:
        op = guard_operation(session, ctx.operation_id, ctx.lease, context=True)
        op.retrieval_ms = (op.retrieval_ms or 0) + duration

import hashlib
import hmac
import json
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import select

from app.domain_actions import handle_callback
from app.memory_resolution import bind_shown_delivery
from app.models import Invocation, Job, MemoryContext, Operation, Update, now
from app.privacy import guard_invocation, guard_operation, owner_lock
from app.queue import LeaseLost, acknowledge, claim, enqueue_text
from app.spike_atelier.capabilities import capabilities
from app.spike_atelier.receipts import SpikeReceipt
from app.terminal import ExecutionExpired, terminal_error
from app.tools import ToolContext


class BindingRejected(Exception):
    pass


class SpikeService:
    def __init__(self, sessions, embeddings, config):
        self.sessions, self.config = sessions, config
        self.tools = capabilities(sessions, embeddings)

    def require_binding_gate(self):
        if not self.config.binding_verified or not self.config.binding_evidence.strip():
            raise BindingRejected("binding_not_verified")

    def _token(self, operation_id):
        raw = str(operation_id)
        signature = hmac.new(
            self.config.binding_secret.get_secret_value().encode(), raw.encode(), hashlib.sha256
        ).hexdigest()
        return raw + "." + signature

    def _operation_id(self, binding):
        try:
            raw, _ = binding.split(".", 1)
            operation_id = UUID(raw)
            if not hmac.compare_digest(binding, self._token(operation_id)):
                raise ValueError()
        except (ValueError, AttributeError):
            raise BindingRejected("invalid_event_binding") from None
        return operation_id

    def ingest(self, payload):
        """Вызывается только аутентифицированным transport, не MCP клиентом."""
        self.require_binding_gate()
        if not isinstance(payload, dict):
            raise BindingRejected("invalid_event")
        update_id = payload.get("update_id")
        message = payload.get("message")
        callback = payload.get("callback_query")
        if type(update_id) is not int or bool(message) == bool(callback):
            raise BindingRejected("invalid_event")
        if not isinstance(callback or message, dict):
            raise BindingRejected("invalid_event")
        source = callback if callback else message
        source_message = callback.get("message") if callback else message
        if not isinstance(source_message, dict):
            raise BindingRejected("invalid_event")
        chat = source_message.get("chat", {})
        if not isinstance(source.get("from"), dict) or not isinstance(chat, dict):
            raise BindingRejected("invalid_event")
        if (
            type(source["from"].get("id")) is not int
            or type(chat.get("id")) is not int
            or source.get("from", {}).get("id") != self.config.owner_id
            or chat.get("id") != self.config.chat_id
            or chat.get("type") != "private"
        ):
            raise BindingRejected("foreign_event")
        if callback:
            if not isinstance(callback.get("id"), str) or not callback["id"]:
                raise BindingRejected("invalid_callback")
            reference = now()
        else:
            if type(message.get("date")) is not int or not isinstance(message.get("text"), str):
                raise BindingRejected("invalid_message")
            try:
                reference = datetime.fromtimestamp(message["date"], UTC)
            except (ValueError, OverflowError, OSError):
                raise BindingRejected("invalid_message_time") from None
        with self.sessions.begin() as session:
            state = owner_lock(session, self.config.owner_id)
            if callback:
                try:
                    prefix, raw_id, choice = callback.get("data", "").split(":")
                    approval_id = UUID(raw_id)
                except (ValueError, AttributeError):
                    raise BindingRejected("invalid_callback") from None
                if prefix != "a" or choice not in {"y", "n"}:
                    raise BindingRejected("invalid_callback")
                receipt = session.scalar(
                    select(SpikeReceipt).where(
                        SpikeReceipt.approval_id == approval_id,
                        SpikeReceipt.owner_id == self.config.owner_id,
                        SpikeReceipt.chat_id == self.config.chat_id,
                        SpikeReceipt.message_id == callback.get("message", {}).get("message_id"),
                    )
                )
                if not receipt:
                    raise BindingRejected("approval_not_delivered")
            existing = session.get(Update, update_id)
            if existing:
                # Redacted/revoked source events may never mint a fresh binding.
                if existing.owner_id != self.config.owner_id or existing.payload != payload:
                    raise BindingRejected("event_replay_mismatch")
                op = session.scalar(select(Operation).where(Operation.update_id == update_id))
                if not op or op.scenario not in {"spike_event", "spike_callback"}:
                    raise BindingRejected("foreign_operation")
                return op.id, None if callback else self._token(op.id)
            session.add(Update(id=update_id, owner_id=self.config.owner_id, payload=payload))
            session.flush()
            op = Operation(
                update_id=update_id,
                owner_id=self.config.owner_id,
                chat_id=self.config.chat_id,
                timezone=self.config.timezone,
                scenario="spike_callback" if callback else "spike_event",
                reference_at=reference,
                source_message_at=reference,
                context_epoch=state.context_epoch,
                deadline_at=now() + timedelta(seconds=90),
            )
            session.add(op)
            session.flush()
            if callback:
                session.add(
                    Job(
                        key=f"spike:callback:{update_id}",
                        operation_id=op.id,
                        kind="callback",
                        payload=payload,
                    )
                )
            return op.id, None if callback else self._token(op.id)

    def schemas(self):
        return [
            {
                "name": name,
                "description": tool.description,
                "inputSchema": tool.arguments.model_json_schema(),
            }
            for name, tool in self.tools.items()
        ]

    def _context(self, session, operation_id, invocation_id):
        op = guard_operation(session, operation_id, context=True)
        if (
            op.scenario != "spike_event"
            or op.owner_id != self.config.owner_id
            or op.chat_id != self.config.chat_id
        ):
            raise BindingRejected("foreign_operation")
        update = session.get(Update, op.update_id)
        if not update or not update.payload:
            raise BindingRejected("source_retired")
        return ToolContext(
            owner_id=op.owner_id,
            chat_id=op.chat_id,
            operation_id=op.id,
            reference_at=op.reference_at,
            timezone=op.timezone,
            idempotency_key=str(invocation_id),
            source_update=update.payload,
        )

    async def call(self, binding, name, arguments):
        self.require_binding_gate()
        started = time.monotonic()
        operation_id = self._operation_id(binding)
        if name not in self.tools:
            raise ValueError("unsupported_tool")
        tool = self.tools[name]
        args = tool.arguments.model_validate(arguments)
        stored_args = args.model_dump(mode="json")
        encoded = json.dumps(stored_args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        # Stable identity comes from the trusted event and exact validated request, not call_id.
        invocation_id = uuid5(operation_id, name + ":" + encoded)
        with self.sessions.begin() as session:
            ctx = self._context(session, operation_id, invocation_id)
            guard_invocation(session, ctx)
            inv = session.get(Invocation, invocation_id)
            if inv:
                if inv.name != name or inv.arguments != stored_args:
                    raise BindingRejected("invocation_retired")
                if inv.result is not None:
                    return inv.result
            else:
                ordinal = session.scalar(
                    select(Invocation.ordinal)
                    .where(Invocation.operation_id == operation_id)
                    .order_by(Invocation.ordinal.desc())
                    .limit(1)
                )
                inv = Invocation(
                    ordinal=(ordinal + 1) if ordinal is not None else 0,
                    id=invocation_id,
                    operation_id=operation_id,
                    call_id=str(invocation_id),
                    name=name,
                    arguments=stored_args,
                )
                session.add(inv)
        prepared = await tool.prepare(ctx, args)
        with self.sessions.begin() as session:
            ctx = self._context(session, operation_id, invocation_id)
            guard_invocation(session, ctx)
            inv = session.get(Invocation, invocation_id)
            if inv.result is not None:
                return inv.result
            result = tool.apply(session, ctx, args, prepared)
            inv.result = result.model_dump(mode="json")
            op = session.get(Operation, operation_id)
            prefix = f"spike:{inv.id}:reply"
            if name == "search_memory" and result.data.get("shown_pairs"):
                context = session.get(MemoryContext, UUID(result.data["context_id"]))
                records = result.data["shown_pairs"]
                text = "Конфликт сохранённых версий:\n\n" + "\n\n".join(
                    f"{pair['conflict_ref']}\na: {pair['a']}\nb: {pair['b']}" for pair in records
                )
                enqueue_text(session, op, text, key_prefix=prefix)
                bind_shown_delivery(session, context, op, prefix)
            elif result.user_message:
                enqueue_text(session, op, result.user_message, result.buttons, key_prefix=prefix)
            op.status = "done"
            final_result = inv.result
        logging.getLogger("spike.tools").info(
            "tool_applied tool=%s invocation=%s status=%s latency_ms=%d",
            name,
            invocation_id,
            result.status,
            int((time.monotonic() - started) * 1000),
        )
        return final_result

    async def callbacks_once(self):
        self.require_binding_gate()
        with self.sessions.begin() as session:
            job = claim(session, Job, self.config.owner_id, include_kinds={"callback"})
        if not job:
            return False
        try:
            await handle_callback(self.sessions, job, (job.id, job.lease_token))
            with self.sessions.begin() as session:
                acknowledge(session, Job, job.id, job.lease_token)
        except ExecutionExpired:
            with self.sessions.begin() as session:
                op = session.get(Operation, job.operation_id)
                owner_lock(session, op.owner_id)
                terminal_error(session, op, "deadline")
        except Exception:
            with self.sessions.begin() as session:
                with suppress(LeaseLost):
                    acknowledge(session, Job, job.id, job.lease_token, error="callback_failed")
            raise
        return True

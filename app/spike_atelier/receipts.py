from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as SQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SpikeReceipt(Base):
    __tablename__ = "spike_telegram_receipts"
    outbox_id: Mapped[UUID] = mapped_column(SQLUUID, ForeignKey("outbox.id"), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    approval_id: Mapped[UUID | None] = mapped_column(SQLUUID)

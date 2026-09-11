from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # 0-1, how confident we are this fact is accurate. NULL means unscored
    # (e.g. a fact the user stated directly via the API, taken at face value).
    confidence: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    # Provenance: which conversation/message this memory was derived from,
    # if any. NULL for memories written directly via the API.
    conversation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("conversations.id"),
        nullable=True,
    )

    message_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("messages.id"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

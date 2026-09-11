from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ToolCallStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ToolCallRecord(Base):
    """Audit record of one tool invocation made during a chat turn.

    This is an audit/debug record, not a knowledge source: it is never
    read back into a chat prompt, RAG context, or memory. Its purpose is
    reconstructing what a tool call actually did, not informing future
    answers.
    """

    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id"),
        nullable=False,
        index=True,
    )

    # The assistant Message this call served. Nullable because the call
    # happens mid-loop, before that Message exists yet - ChatService
    # backfills this once the final reply is persisted, so it's only
    # ever transiently null (for the duration of one send_message call).
    message_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("messages.id"),
        nullable=True,
        index=True,
    )

    tool_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    # Ollama's tool_calls carry no native call id, so (iteration,
    # call_index) is what identifies a call's position within one
    # send_message() turn: iteration = which pass of the tool loop
    # (1-based), call_index = position within that pass's tool_calls
    # list (0-based).
    iteration: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    call_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    # Arguments as supplied by the model. Not size-bounded at the model
    # level (arguments are expected to stay small - query strings, ints),
    # but callers should still avoid ever handing genuinely large or
    # sensitive blobs to a tool's arguments in the first place.
    arguments: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )

    status: Mapped[ToolCallStatus] = mapped_column(
        SQLEnum(ToolCallStatus, name="tool_call_status"),
        nullable=False,
    )

    # The tool's result text, truncated to a bounded length for storage
    # (see ChatService.MAX_TOOL_RESULT_LENGTH) - the full, untruncated
    # result is still what's fed back to the model; only the persisted
    # audit copy is capped. result_truncated records whether that
    # happened, so a truncated audit row is never mistaken for complete.
    result: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    result_truncated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    duration_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

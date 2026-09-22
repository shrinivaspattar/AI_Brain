from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ChatAttachment(Base):
    """A file a person dropped into the chat itself for one-off review - a
    completely separate, ephemeral concept from the Chain 1/2 ingestion
    pipeline (SourceInstance/Document/DocumentChunk). Nothing here is
    embedded, chunked, or made searchable; a chat turn that references it
    (see ChatRequest.attachment_ids) just gets its extracted_text pasted
    into that one turn's prompt.

    Not linked to a Conversation: the file can be uploaded before a
    conversation exists yet (a brand-new chat), and is referenced purely by
    its own id, one message at a time - never implicitly reused by a later
    message in the same conversation.
    """

    __tablename__ = "chat_attachments"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    # Where the uploaded bytes were saved (documents/chat_uploads/<id>/<name>)
    # - kept only so the file can be inspected/cleaned up later; never read
    # again at chat time (extracted_text is what actually gets used).
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)

    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)

    # NULL means extraction failed or hasn't been attempted - such an
    # attachment cannot be referenced in a chat turn (the API rejects it).
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # True if extracted_text was cut short (see MAX_ATTACHMENT_TEXT_CHARS) -
    # surfaced to the model/user so a truncated read is never silently
    # mistaken for the whole file.
    truncated: Mapped[bool] = mapped_column(nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

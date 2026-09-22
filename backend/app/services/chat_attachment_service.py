from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.ingestion.text_extractor import extract_text
from app.models.chat_attachment import ChatAttachment


class AttachmentTooLargeError(ValueError):
    """The uploaded file exceeds MAX_CHAT_ATTACHMENT_BYTES."""


class AttachmentTextExtractionError(ValueError):
    """extract_text() could not read the file as text (e.g. a binary format
    with no extractor, or genuinely undecodable content). The attachment is
    not stored - a person should be told immediately, not find out only when
    they later try to reference an attachment that silently has no text."""


class ChatAttachmentService:
    """Saves a file uploaded directly into a chat turn and extracts its text,
    entirely separate from the Chain 1/2 ingestion pipeline - nothing here is
    embedded, chunked, or added to the search index. See ChatAttachment for
    the full design note."""

    def __init__(self, db: Session):
        self.db = db

    def save(self, filename: str, content: bytes) -> ChatAttachment:
        if len(content) > settings.MAX_CHAT_ATTACHMENT_BYTES:
            raise AttachmentTooLargeError(
                f"{filename} is {len(content):,} bytes, over the "
                f"{settings.MAX_CHAT_ATTACHMENT_BYTES:,} byte limit"
            )

        attachment_id = str(uuid4())
        folder = settings.CHAT_UPLOADS_DIR / attachment_id
        folder.mkdir(parents=True, exist_ok=True)
        # Never trust the client's filename as a path - keep only its final
        # component, so it cannot escape `folder` (e.g. "../../etc/passwd").
        safe_name = Path(filename).name or "upload"
        stored_path = folder / safe_name
        stored_path.write_bytes(content)

        try:
            text = extract_text(stored_path)
        except Exception as exc:  # noqa: BLE001 - extract_text can raise several library-specific errors
            raise AttachmentTextExtractionError(
                f"could not read {filename} as text: {type(exc).__name__}: {exc}"
            ) from exc

        truncated = len(text) > settings.MAX_CHAT_ATTACHMENT_TEXT_CHARS
        if truncated:
            text = text[: settings.MAX_CHAT_ATTACHMENT_TEXT_CHARS]

        attachment = ChatAttachment(
            id=attachment_id,
            original_filename=safe_name,
            stored_path=str(stored_path),
            byte_size=len(content),
            extracted_text=text,
            truncated=truncated,
        )
        self.db.add(attachment)
        self.db.commit()
        self.db.refresh(attachment)
        return attachment

    def get_many(self, attachment_ids: list[str]) -> list[ChatAttachment]:
        """Returns only the ids that actually exist, silently dropping any
        that don't (e.g. a stale id from an old page) - a chat turn should
        never fail outright just because one attachment reference is stale."""
        if not attachment_ids:
            return []
        found = {
            a.id: a
            for a in self.db.query(ChatAttachment).filter(ChatAttachment.id.in_(attachment_ids)).all()
        }
        return [found[i] for i in attachment_ids if i in found]

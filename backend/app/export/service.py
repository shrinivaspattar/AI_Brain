from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.memory import Memory, MemoryStatus
from app.models.message import Message, MessageRole

FORMAT_VERSION = 1


@dataclass(frozen=True)
class ExportResult:
    snapshot_dir: Path
    conversations: int
    messages: int
    memories: int


class DataExportService:
    """Writes conversations, messages and memories to plain JSON files, and
    restores them into an empty database.

    Decision 0001 makes files the source of truth and the database a
    rebuildable index, but these rows have no file counterpart: dropping the
    database would lose every human-authored memory and all chat history.
    This closes that gap. Each export is a self-contained, timestamped
    snapshot directory; nothing is ever overwritten or deleted.

    Not included: tool-call audit records and everything derived from source
    files (documents, chunks, embeddings), which the ingestion pipelines can
    rebuild.

    Restore is deliberately narrow: it refuses to run unless the three tables
    are empty, preserves ids (memories point at messages by id), and applies
    everything in one transaction.
    """

    def __init__(self, db: Session):
        self.db = db

    def export(self, out_dir: Path) -> ExportResult:
        now = datetime.now(UTC)
        snapshot = Path(out_dir) / now.strftime("%Y%m%dT%H%M%S%fZ")
        (snapshot / "conversations").mkdir(parents=True)

        conversations = list(self.db.scalars(select(Conversation).order_by(Conversation.created_at, Conversation.id)))
        message_count = 0
        for conversation in conversations:
            messages = list(
                self.db.scalars(
                    select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id)
                )
            )
            message_count += len(messages)
            self._write(
                snapshot / "conversations" / f"{conversation.id}.json",
                {
                    "id": conversation.id,
                    "title": conversation.title,
                    "created_at": conversation.created_at.isoformat(),
                    "updated_at": conversation.updated_at.isoformat(),
                    "messages": [
                        {
                            "id": m.id,
                            "role": m.role.value,
                            "content": m.content,
                            "citations": m.citations,
                            "created_at": m.created_at.isoformat(),
                        }
                        for m in messages
                    ],
                },
            )

        memories = list(self.db.scalars(select(Memory).order_by(Memory.id)))
        self._write(
            snapshot / "memories.json",
            [
                {
                    "id": m.id,
                    "content": m.content,
                    "confidence": m.confidence,
                    "status": m.status.value,
                    "conversation_id": m.conversation_id,
                    "message_id": m.message_id,
                    "created_at": m.created_at.isoformat(),
                    "updated_at": m.updated_at.isoformat(),
                }
                for m in memories
            ],
        )

        # Written last: a snapshot without a manifest is an interrupted export.
        self._write(
            snapshot / "manifest.json",
            {
                "format_version": FORMAT_VERSION,
                "exported_at": now.isoformat(),
                "conversations": len(conversations),
                "messages": message_count,
                "memories": len(memories),
            },
        )
        return ExportResult(snapshot, len(conversations), message_count, len(memories))

    def restore(self, snapshot_dir: Path) -> ExportResult:
        snapshot = Path(snapshot_dir)
        manifest_path = snapshot / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"{snapshot} has no manifest.json - not a complete export")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("format_version") != FORMAT_VERSION:
            raise ValueError(f"unsupported export format_version {manifest.get('format_version')!r}")

        existing = {
            "conversations": self.db.scalar(select(func.count()).select_from(Conversation)),
            "messages": self.db.scalar(select(func.count()).select_from(Message)),
            "memories": self.db.scalar(select(func.count()).select_from(Memory)),
        }
        if any(existing.values()):
            raise ValueError(f"refusing to restore into a non-empty database: {existing}")

        conversation_files = sorted((snapshot / "conversations").glob("*.json"))
        memories = json.loads((snapshot / "memories.json").read_text())

        try:
            message_count = 0
            for path in conversation_files:
                data = json.loads(path.read_text())
                self.db.add(
                    Conversation(
                        id=data["id"],
                        title=data["title"],
                        created_at=datetime.fromisoformat(data["created_at"]),
                        updated_at=datetime.fromisoformat(data["updated_at"]),
                    )
                )
            self.db.flush()

            for path in conversation_files:
                data = json.loads(path.read_text())
                for m in data["messages"]:
                    message_count += 1
                    self.db.add(
                        Message(
                            id=m["id"],
                            conversation_id=data["id"],
                            role=MessageRole(m["role"]),
                            content=m["content"],
                            citations=m["citations"],
                            created_at=datetime.fromisoformat(m["created_at"]),
                        )
                    )
            self.db.flush()

            for m in memories:
                self.db.add(
                    Memory(
                        id=m["id"],
                        content=m["content"],
                        confidence=m["confidence"],
                        status=MemoryStatus(m["status"]),
                        conversation_id=m["conversation_id"],
                        message_id=m["message_id"],
                        created_at=datetime.fromisoformat(m["created_at"]),
                        updated_at=datetime.fromisoformat(m["updated_at"]),
                    )
                )
            self.db.flush()

            # Explicit ids bypass the sequences; move them past the restored
            # rows so the next insert cannot collide.
            for table in ("messages", "memories"):
                self.db.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                        f"(SELECT MAX(id) FROM {table})) WHERE EXISTS (SELECT 1 FROM {table})"
                    )
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return ExportResult(snapshot, len(conversation_files), message_count, len(memories))

    @staticmethod
    def _write(path: Path, payload) -> None:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

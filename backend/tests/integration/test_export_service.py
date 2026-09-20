import json

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.export.service import FORMAT_VERSION, DataExportService
from app.models.conversation import Conversation
from app.models.memory import Memory, MemoryStatus
from app.models.message import Message, MessageRole


@pytest.fixture
def db():
    engine = create_engine(make_url(settings.DATABASE_URL).set(database="aibrain_test"))
    with Session(engine) as session:
        yield session


def _wipe(db: Session) -> None:
    """Delete only what the tests in this file create, in FK order."""
    db.rollback()
    db.execute(text("DELETE FROM tool_calls WHERE conversation_id IN (SELECT id FROM conversations WHERE title LIKE 'exporttest-%')"))
    db.execute(text("DELETE FROM memories WHERE content LIKE 'exporttest-%'"))
    db.execute(text("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE title LIKE 'exporttest-%')"))
    db.execute(text("DELETE FROM conversations WHERE title LIKE 'exporttest-%'"))
    db.commit()


def _seed(db: Session):
    conversation = Conversation(title="exporttest-chat")
    db.add(conversation)
    db.flush()
    user = Message(conversation_id=conversation.id, role=MessageRole.USER, content="Do I need to relearn a course? Ünïcode ok")
    assistant = Message(
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT,
        content="Not necessarily.",
        citations=[{"document_chunk_id": 1, "document_title": "note.txt", "source_occurrences": None}],
    )
    db.add_all([user, assistant])
    db.flush()
    approved = Memory(
        content="exporttest-user prefers dark mode",
        confidence=0.8,
        status=MemoryStatus.APPROVED,
        conversation_id=conversation.id,
        message_id=assistant.id,
    )
    pending = Memory(content="exporttest-proposed fact", status=MemoryStatus.PENDING)
    db.add_all([approved, pending])
    db.commit()
    return conversation, user, assistant, approved, pending


def _tables_empty(db: Session) -> bool:
    return all(
        db.scalar(select(func.count()).select_from(model)) == 0 for model in (Conversation, Message, Memory)
    )


def test_export_writes_readable_snapshot(db, tmp_path):
    try:
        conversation, user, assistant, approved, _pending = _seed(db)

        result = DataExportService(db).export(tmp_path)

        manifest = json.loads((result.snapshot_dir / "manifest.json").read_text())
        assert manifest["format_version"] == FORMAT_VERSION
        assert manifest["conversations"] == result.conversations
        assert manifest["memories"] == result.memories

        data = json.loads((result.snapshot_dir / "conversations" / f"{conversation.id}.json").read_text())
        assert [m["id"] for m in data["messages"]] == [user.id, assistant.id]
        assert data["messages"][0]["content"].endswith("Ünïcode ok")
        assert data["messages"][1]["citations"][0]["document_title"] == "note.txt"

        memories = {m["id"]: m for m in json.loads((result.snapshot_dir / "memories.json").read_text())}
        assert memories[approved.id]["message_id"] == assistant.id
        assert memories[approved.id]["status"] == "approved"
    finally:
        _wipe(db)


def test_each_export_is_a_new_snapshot(db, tmp_path):
    first = DataExportService(db).export(tmp_path)
    second = DataExportService(db).export(tmp_path)
    assert first.snapshot_dir != second.snapshot_dir
    assert first.snapshot_dir.is_dir() and second.snapshot_dir.is_dir()


def test_round_trip_restores_identical_rows_and_fixes_sequences(db, tmp_path):
    assert _tables_empty(db), "aibrain_test must be empty: another test is leaking rows"
    try:
        conversation, user, assistant, approved, pending = _seed(db)
        before = {
            "conversation": (conversation.id, conversation.title, conversation.created_at),
            "messages": [(m.id, m.role, m.content, m.citations, m.created_at) for m in (user, assistant)],
            "memories": [
                (m.id, m.content, m.confidence, m.status, m.conversation_id, m.message_id, m.created_at)
                for m in (approved, pending)
            ],
        }
        snapshot = DataExportService(db).export(tmp_path).snapshot_dir

        _wipe(db)
        assert _tables_empty(db)

        DataExportService(db).restore(snapshot)

        restored_conversation = db.get(Conversation, before["conversation"][0])
        assert (restored_conversation.id, restored_conversation.title, restored_conversation.created_at) == before["conversation"]
        restored_messages = [db.get(Message, row[0]) for row in before["messages"]]
        assert [(m.id, m.role, m.content, m.citations, m.created_at) for m in restored_messages] == before["messages"]
        restored_memories = [db.get(Memory, row[0]) for row in before["memories"]]
        assert [
            (m.id, m.content, m.confidence, m.status, m.conversation_id, m.message_id, m.created_at)
            for m in restored_memories
        ] == before["memories"]

        # The sequences moved past the restored ids, so a fresh insert works.
        fresh = Message(conversation_id=before["conversation"][0], role=MessageRole.USER, content="after restore")
        db.add(fresh)
        db.commit()
        assert fresh.id > max(row[0] for row in before["messages"])
    finally:
        _wipe(db)


def test_restore_refuses_a_non_empty_database(db, tmp_path):
    try:
        _seed(db)
        snapshot = DataExportService(db).export(tmp_path).snapshot_dir
        with pytest.raises(ValueError, match="non-empty"):
            DataExportService(db).restore(snapshot)
    finally:
        _wipe(db)


def test_restore_refuses_an_incomplete_snapshot(db, tmp_path):
    (tmp_path / "snap").mkdir()
    with pytest.raises(ValueError, match="no manifest"):
        DataExportService(db).restore(tmp_path / "snap")


def test_restore_refuses_an_unknown_format_version(db, tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "manifest.json").write_text(json.dumps({"format_version": 999}))
    with pytest.raises(ValueError, match="unsupported"):
        DataExportService(db).restore(snap)

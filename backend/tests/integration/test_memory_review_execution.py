"""Full-stack regression/behavior tests for the Memory Review Queue.

Unlike most of tests/integration/ (which call MemoryService directly
against a real database), these go through the real FastAPI HTTP layer
via TestClient, with `get_db` overridden to a real `aibrain_test`
session - the same pattern used for the import-jobs execute fix. This
is what the frontend review queue actually calls, so it's worth
verifying end to end: approve/reject, provenance data flowing through
the API untouched, and that rejecting never mutates the source
Conversation/Message rows a memory was derived from.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.conversation import Conversation
from app.models.memory import Memory, MemoryStatus
from app.models.message import Message, MessageRole


def _test_engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _override_get_db(engine):
    def override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return override


def test_approve_memory_via_real_api_removes_it_from_pending_list() -> None:
    engine = _test_engine()
    app.dependency_overrides[get_db] = _override_get_db(engine)

    with Session(engine) as db:
        memory = Memory(content="A candidate fact.", status=MemoryStatus.PENDING)
        db.add(memory)
        db.commit()
        db.refresh(memory)
        memory_id = memory.id

    try:
        client = TestClient(app)

        pending_before = client.get("/memory", params={"status": "pending"}).json()
        assert any(m["id"] == memory_id for m in pending_before)

        response = client.post(f"/memory/{memory_id}/approve")
        assert response.status_code == 200
        assert response.json()["status"] == "approved"

        pending_after = client.get("/memory", params={"status": "pending"}).json()
        assert not any(m["id"] == memory_id for m in pending_after)

        approved = client.get("/memory", params={"status": "approved"}).json()
        assert any(m["id"] == memory_id for m in approved)

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            db.query(Memory).filter(Memory.id == memory_id).delete(
                synchronize_session=False
            )
            db.commit()


def test_reject_memory_via_real_api_does_not_modify_source_data() -> None:
    """The core safety property of rejection: the Memory row is kept
    (not deleted) and flipped to REJECTED, but the Conversation/Message
    it was derived from - its provenance - is completely untouched."""
    engine = _test_engine()
    app.dependency_overrides[get_db] = _override_get_db(engine)

    with Session(engine) as db:
        conversation = Conversation()
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

        message = Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content="Got it, I'll remember that.",
            created_at=datetime.now(UTC),
        )
        db.add(message)
        db.commit()
        db.refresh(message)

        memory = Memory(
            content="A hallucinated or unwanted fact.",
            status=MemoryStatus.PENDING,
            conversation_id=conversation.id,
            message_id=message.id,
        )
        db.add(memory)
        db.commit()
        db.refresh(memory)

        memory_id = memory.id
        conversation_id = conversation.id
        message_id = message.id
        original_message_content = message.content

    try:
        client = TestClient(app)

        response = client.post(f"/memory/{memory_id}/reject")
        assert response.status_code == 200
        assert response.json()["status"] == "rejected"

        with Session(engine) as db:
            # Memory kept, not deleted, correctly REJECTED.
            reviewed = db.get(Memory, memory_id)
            assert reviewed is not None
            assert reviewed.status == MemoryStatus.REJECTED
            assert reviewed.content == "A hallucinated or unwanted fact."

            # Source data completely untouched.
            source_message = db.get(Message, message_id)
            assert source_message is not None
            assert source_message.content == original_message_content

            source_conversation = db.get(Conversation, conversation_id)
            assert source_conversation is not None

        # Provenance still resolvable through the existing chat history
        # endpoint, exactly as the frontend's "View source message"
        # feature relies on.
        history_response = client.get(f"/chat/{conversation_id}")
        assert history_response.status_code == 200
        history = history_response.json()
        assert any(m["id"] == message_id for m in history)

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            db.query(Memory).filter(Memory.id == memory_id).delete(
                synchronize_session=False
            )
            db.query(Message).filter(Message.conversation_id == conversation_id).delete(
                synchronize_session=False
            )
            db.query(Conversation).filter(Conversation.id == conversation_id).delete(
                synchronize_session=False
            )
            db.commit()


def test_approve_memory_returns_404_for_nonexistent_id_via_real_api() -> None:
    engine = _test_engine()
    app.dependency_overrides[get_db] = _override_get_db(engine)

    try:
        client = TestClient(app)
        response = client.post("/memory/999999999/approve")

        assert response.status_code == 404

    finally:
        app.dependency_overrides.clear()


def test_reject_memory_returns_404_for_nonexistent_id_via_real_api() -> None:
    engine = _test_engine()
    app.dependency_overrides[get_db] = _override_get_db(engine)

    try:
        client = TestClient(app)
        response = client.post("/memory/999999999/reject")

        assert response.status_code == 404

    finally:
        app.dependency_overrides.clear()


def test_reviewing_an_already_reviewed_memory_via_real_api_still_succeeds() -> None:
    """Real-HTTP confirmation of the documented last-write-wins behavior:
    approving an already-rejected memory (or vice versa) is not blocked
    by the API - it succeeds and overwrites the prior decision."""
    engine = _test_engine()
    app.dependency_overrides[get_db] = _override_get_db(engine)

    with Session(engine) as db:
        memory = Memory(content="A candidate fact.", status=MemoryStatus.PENDING)
        db.add(memory)
        db.commit()
        db.refresh(memory)
        memory_id = memory.id

    try:
        client = TestClient(app)

        first = client.post(f"/memory/{memory_id}/reject")
        assert first.status_code == 200
        assert first.json()["status"] == "rejected"

        second = client.post(f"/memory/{memory_id}/approve")
        assert second.status_code == 200
        assert second.json()["status"] == "approved"

        final = client.get("/memory", params={"status": "approved"}).json()
        assert any(m["id"] == memory_id for m in final)

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            db.query(Memory).filter(Memory.id == memory_id).delete(
                synchronize_session=False
            )
            db.commit()


def test_pending_memory_list_includes_provenance_fields_via_real_api() -> None:
    """The data path the frontend's provenance panel depends on: a
    pending memory's conversation_id/message_id flow through GET /memory
    unchanged, and the referenced message (with its citations) is
    reachable via the existing GET /chat/{conversation_id} endpoint."""
    engine = _test_engine()
    app.dependency_overrides[get_db] = _override_get_db(engine)

    with Session(engine) as db:
        conversation = Conversation()
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

        message = Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content="Noted: the user's favorite color is blue.",
            citations=[
                {
                    "document_chunk_id": 1,
                    "document_id": "doc-1",
                    "document_title": "preferences.txt",
                    "document_source": "/documents/preferences.txt",
                }
            ],
            created_at=datetime.now(UTC),
        )
        db.add(message)
        db.commit()
        db.refresh(message)

        memory = Memory(
            content="The user's favorite color is blue.",
            confidence=0.85,
            status=MemoryStatus.PENDING,
            conversation_id=conversation.id,
            message_id=message.id,
        )
        db.add(memory)
        db.commit()
        db.refresh(memory)

        memory_id = memory.id
        conversation_id = conversation.id
        message_id = message.id

    try:
        client = TestClient(app)

        pending = client.get("/memory", params={"status": "pending"}).json()
        listed = next(m for m in pending if m["id"] == memory_id)
        assert listed["conversation_id"] == conversation_id
        assert listed["message_id"] == message_id
        assert listed["confidence"] == 0.85

        history = client.get(f"/chat/{conversation_id}").json()
        source_message = next(m for m in history if m["id"] == message_id)
        assert source_message["citations"][0]["document_title"] == "preferences.txt"

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            db.query(Memory).filter(Memory.id == memory_id).delete(
                synchronize_session=False
            )
            db.query(Message).filter(Message.conversation_id == conversation_id).delete(
                synchronize_session=False
            )
            db.query(Conversation).filter(Conversation.id == conversation_id).delete(
                synchronize_session=False
            )
            db.commit()

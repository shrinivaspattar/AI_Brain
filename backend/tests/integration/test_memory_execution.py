from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.schemas.memory import MemoryCreate
from app.services.chat_service import ChatService


def test_memory_service_persists_against_real_database() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        service = MemoryService(db)
        memory = None

        try:
            memory = service.create_memory(
                MemoryCreate(content="User prefers dark mode.", confidence=0.9)
            )

            assert memory.id is not None
            assert memory.content == "User prefers dark mode."
            assert memory.confidence == 0.9

            fetched = service.get_memory(memory.id)
            assert fetched is not None
            assert fetched.content == "User prefers dark mode."

            memories = service.list_memories()
            assert any(m.id == memory.id for m in memories)

        finally:
            if memory is not None:
                db.query(Memory).filter(Memory.id == memory.id).delete()
                db.commit()


def test_memory_service_delete_removes_row() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        service = MemoryService(db)

        memory = service.create_memory(MemoryCreate(content="temporary fact"))
        memory_id = memory.id

        service.delete_memory(memory_id)

        assert service.get_memory(memory_id) is None


def test_chat_prompt_includes_real_memories_from_database() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        memory_service = MemoryService(db)
        memory = memory_service.create_memory(
            MemoryCreate(content="User's name is Shrinivas.")
        )

        retrieval_service = MagicMock()
        retrieval_service.search.return_value = []

        chat_client = MagicMock()
        chat_reply = MagicMock()
        chat_reply.content = "Hi Shrinivas!"
        chat_reply.tool_calls = None
        chat_client.chat.return_value = chat_reply

        service = ChatService(
            db,
            chat_client=chat_client,
            retrieval_service=retrieval_service,
        )

        reply = None

        try:
            reply = service.send_message("hello")

            prompt = chat_client.chat.call_args.args[0]
            assert "User's name is Shrinivas." in prompt[0]["content"]

        finally:
            if reply is not None:
                from app.models.conversation import Conversation
                from app.models.message import Message

                db.query(Message).filter(
                    Message.conversation_id == reply.conversation_id
                ).delete(synchronize_session=False)
                db.query(Conversation).filter(
                    Conversation.id == reply.conversation_id
                ).delete(synchronize_session=False)

            db.query(Memory).filter(Memory.id == memory.id).delete()
            db.commit()

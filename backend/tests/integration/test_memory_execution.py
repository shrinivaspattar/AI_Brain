from unittest.mock import MagicMock

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.memory.service import MemoryService
from app.models.conversation import Conversation
from app.models.memory import Memory, MemoryStatus
from app.models.message import Message
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


def test_memory_service_approve_and_reject_against_real_database() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        service = MemoryService(db)

        proposed = service.propose_memory(content="A proposed fact.")
        assert proposed.status == MemoryStatus.PENDING

        try:
            approved = service.approve_memory(proposed.id)
            assert approved.status == MemoryStatus.APPROVED

            rejected_source = service.propose_memory(content="Another proposal.")
            rejected = service.reject_memory(rejected_source.id)
            assert rejected.status == MemoryStatus.REJECTED

            pending = service.list_memories(status=MemoryStatus.PENDING)
            assert proposed.id not in {m.id for m in pending}
            assert rejected_source.id not in {m.id for m in pending}

            approved_only = service.list_memories(status=MemoryStatus.APPROVED)
            assert proposed.id in {m.id for m in approved_only}

        finally:
            db.query(Memory).filter(
                Memory.id.in_([proposed.id, rejected_source.id])
            ).delete(synchronize_session=False)
            db.commit()


def test_send_message_proposes_and_links_memory_via_real_registry() -> None:
    """End-to-end through the real (non-overridden) tool registry and a
    real database: the `remember` tool creates a genuinely PENDING
    Memory row, excluded from a subsequent chat turn's context until
    approved.
    """
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        retrieval_service = MagicMock()
        retrieval_service.search.return_value = []

        tool_call = MagicMock()
        tool_call.function.name = "remember"
        tool_call.function.arguments = {"content": "The user's name is Alex."}

        tool_call_message = MagicMock()
        tool_call_message.content = ""
        tool_call_message.tool_calls = [tool_call]

        final_message = MagicMock()
        final_message.content = "Got it, I'll remember that."
        final_message.tool_calls = None

        second_turn_message = MagicMock()
        second_turn_message.content = "I don't have that on file yet."
        second_turn_message.tool_calls = None

        chat_client = MagicMock()
        chat_client.chat.side_effect = [
            tool_call_message,
            final_message,
            second_turn_message,
        ]

        service = ChatService(
            db,
            chat_client=chat_client,
            retrieval_service=retrieval_service,
        )

        reply = None
        memory_service = MemoryService(db)

        try:
            reply = service.send_message("My name is Alex.")

            assert reply.content == "Got it, I'll remember that."

            proposed = list(
                db.scalars(
                    select(Memory).where(
                        Memory.conversation_id == reply.conversation_id
                    )
                )
            )

            assert len(proposed) == 1
            memory = proposed[0]
            assert memory.content == "The user's name is Alex."
            assert memory.status == MemoryStatus.PENDING
            assert memory.message_id == reply.id

            # a PENDING proposal must not leak into the next turn's context
            second_reply = service.send_message(
                "What is my name?",
                conversation_id=reply.conversation_id,
            )
            second_prompt = chat_client.chat.call_args_list[-1].args[0]
            assert "Alex" not in second_prompt[0]["content"]

            # approving it makes it available going forward
            memory_service.approve_memory(memory.id)
            approved_memories = memory_service.list_memories(
                status=MemoryStatus.APPROVED
            )
            assert any(m.id == memory.id for m in approved_memories)

        finally:
            if reply is not None:
                from app.models.tool_call import ToolCallRecord

                db.query(Memory).filter(
                    Memory.conversation_id == reply.conversation_id
                ).delete(synchronize_session=False)
                db.query(ToolCallRecord).filter(
                    ToolCallRecord.conversation_id == reply.conversation_id
                ).delete(synchronize_session=False)
                db.query(Message).filter(
                    Message.conversation_id == reply.conversation_id
                ).delete(synchronize_session=False)
                db.query(Conversation).filter(
                    Conversation.id == reply.conversation_id
                ).delete(synchronize_session=False)
            db.commit()

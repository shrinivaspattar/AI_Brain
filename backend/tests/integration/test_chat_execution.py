from unittest.mock import MagicMock

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.message import Message, MessageRole
from app.models.tool_call import ToolCallRecord, ToolCallStatus
from app.services.chat_service import ChatService
from app.services.document_service import DocumentService
from app.schemas.document import DocumentCreate


def fake_chat_client(reply: str) -> MagicMock:
    client = MagicMock()
    message = MagicMock()
    message.content = reply
    message.tool_calls = None
    client.chat.return_value = message
    return client


def fake_tool_calling_chat_client(
    tool_name: str,
    arguments: dict,
    final_reply: str,
) -> MagicMock:
    """A chat client that requests one tool call, then gives a final reply."""
    client = MagicMock()

    tool_call = MagicMock()
    tool_call.function.name = tool_name
    tool_call.function.arguments = arguments

    tool_call_message = MagicMock()
    tool_call_message.content = ""
    tool_call_message.tool_calls = [tool_call]

    final_message = MagicMock()
    final_message.content = final_reply
    final_message.tool_calls = None

    client.chat.side_effect = [tool_call_message, final_message]
    return client


def test_send_message_persists_conversation_and_messages() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        retrieval_service = MagicMock()
        retrieval_service.search.return_value = []

        service = ChatService(
            db,
            chat_client=fake_chat_client("Hello! How can I help?"),
            retrieval_service=retrieval_service,
        )

        conversation = None

        try:
            reply = service.send_message("hi there")

            assert reply.role == MessageRole.ASSISTANT
            assert reply.content == "Hello! How can I help?"
            assert reply.citations is None

            conversation = db.get(Conversation, reply.conversation_id)
            assert conversation is not None

            messages = list(
                db.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at)
                )
            )

            assert len(messages) == 2
            assert messages[0].role == MessageRole.USER
            assert messages[0].content == "hi there"
            assert messages[1].role == MessageRole.ASSISTANT

            # a second message in the same conversation should see history
            reply2 = service.send_message(
                "and now?",
                conversation_id=conversation.id,
            )

            prompt = service.chat_client.chat.call_args.args[0]
            roles_and_content = [(m["role"], m["content"]) for m in prompt[1:]]

            assert ("user", "hi there") in roles_and_content
            assert ("assistant", "Hello! How can I help?") in roles_and_content
            assert ("user", "and now?") in roles_and_content

            assert reply2.conversation_id == conversation.id

        finally:
            if conversation is not None:
                db.query(Message).filter(
                    Message.conversation_id == conversation.id
                ).delete(synchronize_session=False)
                db.query(Conversation).filter(
                    Conversation.id == conversation.id
                ).delete(synchronize_session=False)
                db.commit()


def test_send_message_cites_retrieved_chunks() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        document_service = DocumentService(db)
        document = document_service.create_document(
            DocumentCreate(
                title="brain-notes.txt",
                source="/chat-test/brain-notes.txt",
                source_type="txt",
            )
        )

        chunk = DocumentChunk(
            document_id=document.id,
            chunk_index=0,
            content="AI_Brain runs fully offline.",
            embedding=[0.1] * settings.EMBEDDING_DIMENSIONS,
        )
        db.add(chunk)
        db.commit()
        db.refresh(chunk)

        retrieval_service = MagicMock()
        from app.rag.retrieval_service import RetrievedChunk

        retrieval_service.search.return_value = [
            RetrievedChunk(chunk=chunk, document=document, distance=0.05)
        ]

        service = ChatService(
            db,
            chat_client=fake_chat_client("It runs offline, per [1]."),
            retrieval_service=retrieval_service,
        )

        reply = None

        try:
            reply = service.send_message("does it run offline?")

            assert reply.citations == [
                {
                    "document_chunk_id": chunk.id,
                    "document_id": document.id,
                    "document_title": "brain-notes.txt",
                    "document_source": "/chat-test/brain-notes.txt",
                    "source_occurrences": None,
                }
            ]

        finally:
            if reply is not None:
                db.query(Message).filter(
                    Message.conversation_id == reply.conversation_id
                ).delete(synchronize_session=False)
                db.query(Conversation).filter(
                    Conversation.id == reply.conversation_id
                ).delete(synchronize_session=False)
            db.query(DocumentChunk).filter(
                DocumentChunk.document_id == document.id
            ).delete(synchronize_session=False)
            db.query(Document).filter(Document.id == document.id).delete(
                synchronize_session=False
            )
            db.commit()


def _cleanup_conversation(db: Session, conversation_id: str) -> None:
    db.query(ToolCallRecord).filter(
        ToolCallRecord.conversation_id == conversation_id
    ).delete(synchronize_session=False)
    db.query(Message).filter(
        Message.conversation_id == conversation_id
    ).delete(synchronize_session=False)
    db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).delete(synchronize_session=False)
    db.commit()


def test_send_message_persists_tool_call_audit_record_for_successful_call() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        retrieval_service = MagicMock()
        retrieval_service.search.return_value = []

        # No tool_registry override: exercises the real
        # get_current_datetime tool, not a mock.
        service = ChatService(
            db,
            chat_client=fake_tool_calling_chat_client(
                "get_current_datetime", {}, "It's currently 2026."
            ),
            retrieval_service=retrieval_service,
        )

        reply = None

        try:
            reply = service.send_message("what time is it?")

            assert reply.content == "It's currently 2026."

            records = list(
                db.scalars(
                    select(ToolCallRecord).where(
                        ToolCallRecord.conversation_id == reply.conversation_id
                    )
                )
            )

            assert len(records) == 1
            record = records[0]

            assert record.tool_name == "get_current_datetime"
            assert record.message_id == reply.id
            assert record.conversation_id == reply.conversation_id
            assert record.iteration == 1
            assert record.call_index == 0
            assert record.arguments == {}
            assert record.status == ToolCallStatus.SUCCESS
            assert record.error_message is None
            assert record.result is not None
            assert record.result_truncated is False
            assert record.duration_ms is not None
            assert record.duration_ms >= 0
            assert record.created_at is not None

        finally:
            if reply is not None:
                _cleanup_conversation(db, reply.conversation_id)


def test_send_message_persists_tool_call_audit_record_for_unknown_tool() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        retrieval_service = MagicMock()
        retrieval_service.search.return_value = []

        service = ChatService(
            db,
            chat_client=fake_tool_calling_chat_client(
                "does_not_exist", {"x": 1}, "I couldn't do that."
            ),
            retrieval_service=retrieval_service,
        )

        reply = None

        try:
            reply = service.send_message("do the impossible thing")

            records = list(
                db.scalars(
                    select(ToolCallRecord).where(
                        ToolCallRecord.conversation_id == reply.conversation_id
                    )
                )
            )

            assert len(records) == 1
            record = records[0]

            assert record.tool_name == "does_not_exist"
            assert record.status == ToolCallStatus.ERROR
            assert record.error_message is not None
            assert "unknown tool" in record.error_message
            assert record.message_id == reply.id

        finally:
            if reply is not None:
                _cleanup_conversation(db, reply.conversation_id)


def test_send_message_persists_multiple_audit_records_across_iterations() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        retrieval_service = MagicMock()
        retrieval_service.search.return_value = []

        first_tool_call = MagicMock()
        first_tool_call.function.name = "get_current_datetime"
        first_tool_call.function.arguments = {}

        second_tool_call = MagicMock()
        second_tool_call.function.name = "list_recent_documents"
        second_tool_call.function.arguments = {"limit": 3}

        first_message = MagicMock()
        first_message.content = ""
        first_message.tool_calls = [first_tool_call]

        second_message = MagicMock()
        second_message.content = ""
        second_message.tool_calls = [second_tool_call]

        final_message = MagicMock()
        final_message.content = "Done checking both."
        final_message.tool_calls = None

        chat_client = MagicMock()
        chat_client.chat.side_effect = [first_message, second_message, final_message]

        service = ChatService(
            db,
            chat_client=chat_client,
            retrieval_service=retrieval_service,
        )

        reply = None

        try:
            reply = service.send_message("check the time and my documents")

            assert reply.content == "Done checking both."

            records = list(
                db.scalars(
                    select(ToolCallRecord)
                    .where(ToolCallRecord.conversation_id == reply.conversation_id)
                    .order_by(ToolCallRecord.iteration)
                )
            )

            assert len(records) == 2
            assert records[0].tool_name == "get_current_datetime"
            assert records[0].iteration == 1
            assert records[1].tool_name == "list_recent_documents"
            assert records[1].iteration == 2
            assert all(r.message_id == reply.id for r in records)

        finally:
            if reply is not None:
                _cleanup_conversation(db, reply.conversation_id)

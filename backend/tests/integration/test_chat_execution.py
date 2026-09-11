from unittest.mock import MagicMock

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.message import Message, MessageRole
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

from unittest.mock import MagicMock

import pytest

from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.message import Message, MessageRole
from app.rag.retrieval_service import RetrievedChunk
from app.services.chat_service import ChatService


def _retrieved_chunk(
    chunk_id: int = 1,
    document_id: str = "doc-1",
    title: str = "notes.txt",
    source: str = "/documents/notes.txt",
    content: str = "AI_Brain is offline-first.",
    distance: float = 0.1,
) -> RetrievedChunk:
    chunk = DocumentChunk(
        id=chunk_id,
        document_id=document_id,
        chunk_index=0,
        content=content,
    )
    document = Document(
        id=document_id,
        title=title,
        source=source,
        source_type="txt",
    )
    return RetrievedChunk(chunk=chunk, document=document, distance=distance)


def test_send_message_creates_conversation_when_none_given() -> None:
    db = MagicMock()
    db.get.return_value = None

    chat_client = MagicMock()
    chat_client.chat.return_value = "Hi! How can I help?"

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    # db.refresh has no return value, so give created rows real ids/timestamps
    def fake_refresh(obj):
        if isinstance(obj, Conversation) and obj.id is None:
            obj.id = "conv-1"
        if isinstance(obj, Message) and obj.id is None:
            obj.id = 1

    db.refresh.side_effect = fake_refresh
    db.scalars.return_value = []

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
    )

    result = service.send_message("hello")

    assert result.role == MessageRole.ASSISTANT
    assert result.content == "Hi! How can I help?"
    assert result.citations is None

    chat_client.chat.assert_called_once()


def test_send_message_raises_for_unknown_conversation() -> None:
    db = MagicMock()
    db.get.return_value = None

    service = ChatService(
        db,
        chat_client=MagicMock(),
        retrieval_service=MagicMock(),
    )

    with pytest.raises(ValueError, match="Conversation conv-404 not found"):
        service.send_message("hello", conversation_id="conv-404")


def test_send_message_uses_existing_conversation() -> None:
    db = MagicMock()
    conversation = Conversation(id="conv-1")
    db.get.return_value = conversation
    db.scalars.return_value = []

    chat_client = MagicMock()
    chat_client.chat.return_value = "reply"

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
    )

    result = service.send_message("hello", conversation_id="conv-1")

    assert result.conversation_id == "conv-1"
    # get_or_create should not have created a *new* Conversation
    added_conversations = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Conversation)
    ]
    assert added_conversations == []


def test_send_message_includes_citations_and_context_in_prompt() -> None:
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []

    def fake_refresh(obj):
        if isinstance(obj, Conversation) and obj.id is None:
            obj.id = "conv-1"
        if isinstance(obj, Message) and obj.id is None:
            obj.id = 1

    db.refresh.side_effect = fake_refresh

    chat_client = MagicMock()
    chat_client.chat.return_value = "According to [1], AI_Brain is offline-first."

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = [_retrieved_chunk()]

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
    )

    result = service.send_message("what is AI_Brain?")

    assert result.citations == [
        {
            "document_chunk_id": 1,
            "document_id": "doc-1",
            "document_title": "notes.txt",
            "document_source": "/documents/notes.txt",
        }
    ]

    prompt = chat_client.chat.call_args.args[0]
    system_message = prompt[0]

    assert system_message["role"] == "system"
    assert "[1] notes.txt: AI_Brain is offline-first." in system_message["content"]


def test_send_message_notes_when_no_context_found() -> None:
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []

    def fake_refresh(obj):
        if isinstance(obj, Conversation) and obj.id is None:
            obj.id = "conv-1"
        if isinstance(obj, Message) and obj.id is None:
            obj.id = 1

    db.refresh.side_effect = fake_refresh

    chat_client = MagicMock()
    chat_client.chat.return_value = "I don't have documents about that."

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
    )

    service.send_message("something obscure")

    prompt = chat_client.chat.call_args.args[0]
    assert "No relevant documents were found" in prompt[0]["content"]

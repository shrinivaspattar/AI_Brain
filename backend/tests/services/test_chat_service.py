from unittest.mock import MagicMock

import pytest

from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.memory import Memory
from app.models.message import Message, MessageRole
from app.rag.retrieval_service import RetrievedChunk
from app.services.chat_service import ChatService


def _reply(content: str) -> MagicMock:
    """A plain (no tool calls) chat_client.chat() response."""
    message = MagicMock()
    message.content = content
    message.tool_calls = None
    return message


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
    chat_client.chat.return_value = _reply("Hi! How can I help?")

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
    chat_client.chat.return_value = _reply("reply")

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
    chat_client.chat.return_value = _reply("According to [1], AI_Brain is offline-first.")

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
    chat_client.chat.return_value = _reply("I don't have documents about that.")

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


def test_send_message_includes_memories_in_prompt() -> None:
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
    chat_client.chat.return_value = _reply("You prefer dark mode, as I recall.")

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    memory_service = MagicMock()
    memory_service.list_memories.return_value = [
        Memory(id=1, content="User prefers dark mode.")
    ]

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
        memory_service=memory_service,
    )

    service.send_message("do you remember my preferences?")

    prompt = chat_client.chat.call_args.args[0]
    system_content = prompt[0]["content"]

    assert "What you know about the user:" in system_content
    assert "- User prefers dark mode." in system_content
    memory_service.list_memories.assert_called_once()


def test_send_message_omits_memory_block_when_no_memories() -> None:
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
    chat_client.chat.return_value = _reply("I don't know that about you yet.")

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    memory_service = MagicMock()
    memory_service.list_memories.return_value = []

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
        memory_service=memory_service,
    )

    service.send_message("what do you know about me?")

    prompt = chat_client.chat.call_args.args[0]
    assert "What you know about the user:" not in prompt[0]["content"]


def _tool_call(name: str, **arguments) -> MagicMock:
    call = MagicMock()
    call.function.name = name
    call.function.arguments = arguments
    return call


def _tool_reply(*calls: MagicMock, content: str = "") -> MagicMock:
    message = MagicMock()
    message.content = content
    message.tool_calls = list(calls)
    return message


def test_send_message_executes_a_tool_call_and_returns_final_reply() -> None:
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
    chat_client.chat.side_effect = [
        _tool_reply(_tool_call("get_current_datetime")),
        _reply("It's currently 2026-09-12."),
    ]

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    tool_registry = MagicMock()
    tool_registry.to_ollama_schema.return_value = [{"type": "function"}]
    tool_registry.call.return_value = "2026-09-12T00:00:00+00:00"

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
        tool_registry=tool_registry,
    )

    result = service.send_message("what time is it?")

    assert result.content == "It's currently 2026-09-12."
    tool_registry.call.assert_called_once_with("get_current_datetime", {})
    assert chat_client.chat.call_count == 2

    second_call_messages = chat_client.chat.call_args_list[1].args[0]
    assert second_call_messages[-2]["role"] == "assistant"
    assert second_call_messages[-2]["tool_calls"][0]["function"]["name"] == (
        "get_current_datetime"
    )
    assert second_call_messages[-1] == {
        "role": "tool",
        "tool_name": "get_current_datetime",
        "content": "2026-09-12T00:00:00+00:00",
    }


def test_send_message_stops_after_max_tool_iterations() -> None:
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
    chat_client.chat.return_value = _tool_reply(_tool_call("search_knowledge_base", query="x"))

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    tool_registry = MagicMock()
    tool_registry.to_ollama_schema.return_value = []
    tool_registry.call.return_value = "some result"

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
        tool_registry=tool_registry,
    )

    result = service.send_message("keep searching forever")

    from app.services.chat_service import MAX_TOOL_ITERATIONS

    assert chat_client.chat.call_count == MAX_TOOL_ITERATIONS
    assert "wasn't able to finish" in result.content


def test_chat_service_defaults_to_builtin_tool_registry() -> None:
    db = MagicMock()

    service = ChatService(db, chat_client=MagicMock(), retrieval_service=MagicMock())

    tool_names = {tool.name for tool in service.tool_registry.list_tools()}
    assert "search_knowledge_base" in tool_names
    assert "get_current_datetime" in tool_names
    assert "list_recent_documents" in tool_names

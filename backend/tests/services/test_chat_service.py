from unittest.mock import MagicMock

import pytest

from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.memory import Memory
from app.models.message import Message, MessageRole
from app.models.tool_call import ToolCallRecord, ToolCallStatus
from app.rag.retrieval_service import RetrievedChunk
from app.services.chat_service import ChatService
from app.tools.registry import ToolCallResult


def _make_fake_refresh():
    """A db.refresh side_effect that assigns ids the way a real commit
    would, for Conversation/Message/ToolCallRecord - so code that reads
    `.id` right after refresh() behaves the same as it would against a
    real database.
    """
    counters = {"message": 0, "tool_call": 0}

    def fake_refresh(obj):
        if isinstance(obj, Conversation) and obj.id is None:
            obj.id = "conv-1"
        if isinstance(obj, Message) and obj.id is None:
            counters["message"] += 1
            obj.id = counters["message"]
        if isinstance(obj, ToolCallRecord) and obj.id is None:
            counters["tool_call"] += 1
            obj.id = counters["tool_call"]

    return fake_refresh


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
    db.refresh.side_effect = _make_fake_refresh()

    chat_client = MagicMock()
    chat_client.chat.side_effect = [
        _tool_reply(_tool_call("get_current_datetime")),
        _reply("It's currently 2026-09-12."),
    ]

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    tool_registry = MagicMock()
    tool_registry.to_ollama_schema.return_value = [{"type": "function"}]
    tool_registry.call.return_value = ToolCallResult(
        content="2026-09-12T00:00:00+00:00",
        is_error=False,
        duration_ms=1,
    )

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


def test_send_message_persists_audit_record_for_successful_tool_call() -> None:
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    chat_client = MagicMock()
    chat_client.chat.side_effect = [
        _tool_reply(_tool_call("get_current_datetime")),
        _reply("It's currently 2026-09-12."),
    ]

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    tool_registry = MagicMock()
    tool_registry.to_ollama_schema.return_value = [{"type": "function"}]
    tool_registry.call.return_value = ToolCallResult(
        content="2026-09-12T00:00:00+00:00",
        is_error=False,
        duration_ms=7,
    )

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
        tool_registry=tool_registry,
    )

    assistant_message = service.send_message("what time is it?")

    added_records = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], ToolCallRecord)
    ]

    assert len(added_records) == 1
    record = added_records[0]

    assert record.conversation_id == "conv-1"
    assert record.tool_name == "get_current_datetime"
    assert record.iteration == 1
    assert record.call_index == 0
    assert record.arguments == {}
    assert record.status == ToolCallStatus.SUCCESS
    assert record.result == "2026-09-12T00:00:00+00:00"
    assert record.result_truncated is False
    assert record.error_message is None
    assert record.duration_ms == 7

    # linked to the assistant message once it exists
    link_filters = db.query.return_value.filter.return_value
    link_filters.update.assert_called_once_with(
        {"message_id": assistant_message.id}, synchronize_session=False
    )


def test_send_message_persists_audit_record_for_failed_tool_call() -> None:
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    chat_client = MagicMock()
    chat_client.chat.side_effect = [
        _tool_reply(_tool_call("search_knowledge_base", query="x")),
        _reply("Sorry, that search failed."),
    ]

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    tool_registry = MagicMock()
    tool_registry.to_ollama_schema.return_value = [{"type": "function"}]
    tool_registry.call.return_value = ToolCallResult(
        content="Error running tool 'search_knowledge_base': boom",
        is_error=True,
        error="boom",
        duration_ms=3,
    )

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
        tool_registry=tool_registry,
    )

    service.send_message("search for something")

    added_records = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], ToolCallRecord)
    ]

    assert len(added_records) == 1
    record = added_records[0]

    assert record.status == ToolCallStatus.ERROR
    assert record.error_message == "boom"
    assert "boom" in record.result


def test_send_message_succeeds_even_if_audit_persistence_fails() -> None:
    """Audit logging is best-effort: a DB failure while persisting a
    ToolCallRecord must not prevent the user from getting an answer.
    """
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    last_added = {"obj": None}
    db.add.side_effect = lambda obj: last_added.__setitem__("obj", obj)

    def maybe_fail_commit():
        if isinstance(last_added["obj"], ToolCallRecord):
            raise RuntimeError("db unavailable")

    db.commit.side_effect = maybe_fail_commit

    chat_client = MagicMock()
    chat_client.chat.side_effect = [
        _tool_reply(_tool_call("get_current_datetime")),
        _reply("It's currently 2026-09-12."),
    ]

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    tool_registry = MagicMock()
    tool_registry.to_ollama_schema.return_value = [{"type": "function"}]
    tool_registry.call.return_value = ToolCallResult(
        content="2026-09-12T00:00:00+00:00",
        is_error=False,
    )

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
        tool_registry=tool_registry,
    )

    result = service.send_message("what time is it?")

    assert result.content == "It's currently 2026-09-12."
    db.rollback.assert_called()


def test_send_message_persists_audit_records_across_multiple_iterations() -> None:
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    chat_client = MagicMock()
    chat_client.chat.side_effect = [
        _tool_reply(_tool_call("get_current_datetime")),
        _tool_reply(_tool_call("list_recent_documents", limit=5)),
        _reply("Here's what I found."),
    ]

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    tool_registry = MagicMock()
    tool_registry.to_ollama_schema.return_value = [{"type": "function"}]
    tool_registry.call.side_effect = [
        ToolCallResult(content="2026-09-12T00:00:00+00:00", is_error=False),
        ToolCallResult(content="notes.txt (txt) - /documents/notes.txt", is_error=False),
    ]

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
        tool_registry=tool_registry,
    )

    service.send_message("what time is it, and what have I imported?")

    added_records = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], ToolCallRecord)
    ]

    assert len(added_records) == 2
    assert (added_records[0].iteration, added_records[0].call_index) == (1, 0)
    assert added_records[0].tool_name == "get_current_datetime"
    assert (added_records[1].iteration, added_records[1].call_index) == (2, 0)
    assert added_records[1].tool_name == "list_recent_documents"


def test_send_message_truncates_large_tool_results_for_audit() -> None:
    from app.services.chat_service import MAX_TOOL_RESULT_LENGTH

    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    huge_result = "x" * (MAX_TOOL_RESULT_LENGTH + 500)

    chat_client = MagicMock()
    chat_client.chat.side_effect = [
        _tool_reply(_tool_call("search_knowledge_base", query="x")),
        _reply("Found a lot of text."),
    ]

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    tool_registry = MagicMock()
    tool_registry.to_ollama_schema.return_value = [{"type": "function"}]
    tool_registry.call.return_value = ToolCallResult(content=huge_result, is_error=False)

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
        tool_registry=tool_registry,
    )

    service.send_message("search for a lot of stuff")

    added_records = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], ToolCallRecord)
    ]

    record = added_records[0]
    assert record.result_truncated is True
    assert len(record.result) == MAX_TOOL_RESULT_LENGTH

    # the model itself must still see the FULL result, not the truncated
    # audit copy
    second_call_messages = chat_client.chat.call_args_list[1].args[0]
    tool_message = second_call_messages[-1]
    assert tool_message["content"] == huge_result


def test_send_message_does_not_truncate_small_tool_results() -> None:
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    chat_client = MagicMock()
    chat_client.chat.side_effect = [
        _tool_reply(_tool_call("get_current_datetime")),
        _reply("Sure."),
    ]

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    tool_registry = MagicMock()
    tool_registry.to_ollama_schema.return_value = [{"type": "function"}]
    tool_registry.call.return_value = ToolCallResult(content="short", is_error=False)

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
        tool_registry=tool_registry,
    )

    service.send_message("hi")

    added_records = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], ToolCallRecord)
    ]

    assert added_records[0].result_truncated is False
    assert added_records[0].result == "short"


def test_send_message_stops_after_max_tool_iterations() -> None:
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    chat_client = MagicMock()
    chat_client.chat.return_value = _tool_reply(_tool_call("search_knowledge_base", query="x"))

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    tool_registry = MagicMock()
    tool_registry.to_ollama_schema.return_value = []
    tool_registry.call.return_value = ToolCallResult(content="some result", is_error=False)

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

    # every iteration's tool call should still get an audit record, even
    # though the loop never converges to a final reply
    added_records = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], ToolCallRecord)
    ]
    assert len(added_records) == MAX_TOOL_ITERATIONS


def test_chat_service_defaults_to_builtin_tool_registry() -> None:
    db = MagicMock()

    service = ChatService(db, chat_client=MagicMock(), retrieval_service=MagicMock())

    tool_names = {tool.name for tool in service.tool_registry.list_tools()}
    assert "search_knowledge_base" in tool_names
    assert "get_current_datetime" in tool_names
    assert "list_recent_documents" in tool_names

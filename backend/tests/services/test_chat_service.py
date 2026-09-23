from unittest.mock import MagicMock, patch

import pytest

from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.memory import Memory, MemoryStatus
from app.models.message import Message, MessageRole
from app.models.tool_call import ToolCallRecord, ToolCallStatus
from app.rag.retrieval_service import RetrievedChunk
from app.services.chat_service import MAX_MEMORIES, ChatService
from app.tools.registry import ToolCallResult


def _make_fake_refresh():
    """A db.refresh side_effect that assigns ids the way a real commit
    would, for Conversation/Message/ToolCallRecord/Memory - so code that
    reads `.id` right after refresh() behaves the same as it would
    against a real database.
    """
    counters = {"message": 0, "tool_call": 0, "memory": 0}

    def fake_refresh(obj):
        if isinstance(obj, Conversation) and obj.id is None:
            obj.id = "conv-1"
        if isinstance(obj, Message) and obj.id is None:
            counters["message"] += 1
            obj.id = counters["message"]
        if isinstance(obj, ToolCallRecord) and obj.id is None:
            counters["tool_call"] += 1
            obj.id = counters["tool_call"]
        if isinstance(obj, Memory) and obj.id is None:
            counters["memory"] += 1
            obj.id = counters["memory"]

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
    # get_or_create should not have created a *new*, different Conversation -
    # the existing one is legitimately re-added later, to persist the
    # updated_at touch on activity (see test_send_message_touches_conversation_updated_at).
    added_conversations = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Conversation)
    ]
    assert all(c is conversation for c in added_conversations)


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
            "source_occurrences": None,
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
    assert "remember" in tool_names


def test_send_message_only_loads_approved_memories() -> None:
    """The chat read-hook must never surface a PENDING (unreviewed)
    memory proposal - only APPROVED memories are safe to inject into
    the prompt.
    """
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("hi")

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

    service.send_message("hello")

    memory_service.list_memories.assert_called_once_with(
        limit=MAX_MEMORIES,
        status=MemoryStatus.APPROVED,
    )


def test_send_message_rebuilds_registry_with_conversation_context() -> None:
    """When no tool_registry override is given, send_message must build a
    fresh one scoped to the current conversation (so `remember` can
    attach the right conversation_id), not reuse whatever was built at
    __init__ time.
    """
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("hi")

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    with patch("app.services.chat_service.build_default_registry") as build_registry:
        fake_registry = MagicMock()
        fake_registry.to_ollama_schema.return_value = []
        build_registry.return_value = fake_registry

        service = ChatService(
            db,
            chat_client=chat_client,
            retrieval_service=retrieval_service,
        )
        build_registry.reset_mock()  # ignore the __init__-time call

        service.send_message("hello")

        build_registry.assert_called_once()
        call_kwargs = build_registry.call_args.kwargs
        assert call_kwargs["conversation_id"] == "conv-1"
        assert callable(call_kwargs["on_memory_proposed"])


def test_send_message_does_not_rebuild_an_overridden_registry() -> None:
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("hi")

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    tool_registry = MagicMock()
    tool_registry.to_ollama_schema.return_value = []

    with patch("app.services.chat_service.build_default_registry") as build_registry:
        service = ChatService(
            db,
            chat_client=chat_client,
            retrieval_service=retrieval_service,
            tool_registry=tool_registry,
        )

        service.send_message("hello")

        build_registry.assert_not_called()
        assert service.tool_registry is tool_registry


def test_send_message_persists_and_links_a_proposed_memory() -> None:
    """Full flow through the real (non-overridden) registry: the model
    calls `remember`, a PENDING Memory is created via the real
    MemoryService, and its message_id is backfilled once the assistant
    Message exists - mirroring how ToolCallRecord linkage works.
    """
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    chat_client = MagicMock()
    chat_client.chat.side_effect = [
        _tool_reply(_tool_call("remember", content="The user's name is Alex.")),
        _reply("Got it, I'll remember that."),
    ]

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
    )

    result = service.send_message("My name is Alex.")

    assert result.content == "Got it, I'll remember that."

    added_memories = [
        call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], Memory)
    ]
    assert len(added_memories) == 1

    memory = added_memories[0]
    assert memory.content == "The user's name is Alex."
    assert memory.status == MemoryStatus.PENDING
    assert memory.conversation_id == "conv-1"

    link_filters = db.query.return_value.filter.return_value
    link_filters.update.assert_any_call(
        {"message_id": result.id}, synchronize_session=False
    )


def test_send_message_succeeds_even_if_memory_proposal_fails() -> None:
    """Mirrors the tool-call-audit resilience guarantee: a DB failure
    while persisting a proposed Memory must not prevent the user from
    getting an answer.
    """
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()

    last_added = {"obj": None}
    db.add.side_effect = lambda obj: last_added.__setitem__("obj", obj)

    def maybe_fail_commit():
        if isinstance(last_added["obj"], Memory):
            raise RuntimeError("db unavailable")

    db.commit.side_effect = maybe_fail_commit

    chat_client = MagicMock()
    chat_client.chat.side_effect = [
        _tool_reply(_tool_call("remember", content="The user's name is Alex.")),
        _reply("Got it, I'll remember that."),
    ]

    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    service = ChatService(
        db,
        chat_client=chat_client,
        retrieval_service=retrieval_service,
    )

    result = service.send_message("My name is Alex.")

    assert result.content == "Got it, I'll remember that."


# ---- offline-chat evaluation follow-ups: search query, relevance cutoff, source names ----

from app.rag.retrieval_service import SourceOccurrence  # noqa: E402


def _message(role: MessageRole, content: str, message_id: int) -> Message:
    return Message(id=message_id, conversation_id="conv-1", role=role, content=content)


def test_short_follow_up_is_searched_together_with_the_previous_user_message() -> None:
    history = [
        _message(MessageRole.USER, "What does my Bangalore to Germany roadmap say?", 1),
        _message(MessageRole.ASSISTANT, "It has phases.", 2),
        _message(MessageRole.USER, "and the timeline?", 3),
    ]

    query = ChatService._search_query("and the timeline?", history, current_message_id=3)

    assert query == "What does my Bangalore to Germany roadmap say? and the timeline?"


def test_long_or_first_messages_are_searched_as_they_are() -> None:
    long_message = "Please summarize everything I wrote about the education system comparison in detail"
    history = [_message(MessageRole.USER, "earlier question", 1), _message(MessageRole.USER, long_message, 2)]

    assert ChatService._search_query(long_message, history, current_message_id=2) == long_message
    assert ChatService._search_query("hi", [_message(MessageRole.USER, "hi", 1)], current_message_id=1) == "hi"


def test_relevance_cutoff_drops_far_chunks_only_when_configured(monkeypatch) -> None:
    near = _retrieved_chunk(chunk_id=1, distance=0.2)
    far = _retrieved_chunk(chunk_id=2, distance=0.6)

    monkeypatch.setattr("app.services.chat_service.settings.CHAT_MAX_SOURCE_DISTANCE", None)
    assert ChatService._relevant_only([near, far]) == [near, far]

    monkeypatch.setattr("app.services.chat_service.settings.CHAT_MAX_SOURCE_DISTANCE", 0.4)
    assert ChatService._relevant_only([near, far]) == [near]


def test_send_message_shows_no_sources_when_nothing_is_relevant(monkeypatch) -> None:
    monkeypatch.setattr("app.services.chat_service.settings.CHAT_MAX_SOURCE_DISTANCE", 0.4)
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("Hello!")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = [_retrieved_chunk(distance=0.7)]

    result = ChatService(db, chat_client=chat_client, retrieval_service=retrieval_service).send_message("hi")

    assert result.citations is None
    assert "No relevant documents were found" in chat_client.chat.call_args.args[0][0]["content"]


def test_source_label_prefers_the_original_file_name_over_the_working_copy_title() -> None:
    from dataclasses import replace

    result = replace(
        _retrieved_chunk(title="content.md", source="documents/workspace/group_9/content.md"),
        source_occurrences=[
            SourceOccurrence(root_t7_path="/master/claude browser offline/039_Bangalore_Germany_Roadmap.md",
                             member_path=None, archive_ancestry=None)
        ],
    )

    assert ChatService._source_label(result) == "039_Bangalore_Germany_Roadmap.md"
    assert ChatService._source_label(_retrieved_chunk(title="notes.txt")) == "notes.txt"


# ---- streaming ----

def _stream_part(content: str = "", tool_calls=None) -> MagicMock:
    part = MagicMock()
    part.content = content
    part.tool_calls = tool_calls
    return part


def _stream_service(parts_per_call, retrieved=None):
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()
    chat_client = MagicMock()
    chat_client.chat_stream.side_effect = [iter(parts) for parts in parts_per_call]
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = retrieved or []
    return ChatService(db, chat_client=chat_client, retrieval_service=retrieval_service), chat_client


def test_send_message_stream_yields_pieces_then_the_saved_message() -> None:
    service, _ = _stream_service([[_stream_part("Hel"), _stream_part("lo "), _stream_part("world")]],
                                 retrieved=[_retrieved_chunk()])

    events = list(service.send_message_stream("hi there"))

    assert [e["type"] for e in events] == ["token", "token", "token", "done"]
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Hello world"
    done = events[-1]["message"]
    assert done.content == "Hello world"
    assert done.citations[0]["document_title"] == "notes.txt"


def test_send_message_stream_runs_a_tool_call_then_streams_the_final_reply() -> None:
    call = _tool_call("get_current_time")
    registry = MagicMock()
    registry.to_ollama_schema.return_value = []
    registry.call.return_value = ToolCallResult(content="12:00", is_error=False)
    service, chat_client = _stream_service([
        [_stream_part("", tool_calls=[call])],
        [_stream_part("It is "), _stream_part("noon.")],
    ])
    service.tool_registry = registry
    service._tool_registry_override = registry

    events = list(service.send_message_stream("what time is it?"))

    assert [e for e in events if e["type"] == "token"] == [
        {"type": "token", "text": "It is "}, {"type": "token", "text": "noon."}]
    assert events[-1]["message"].content == "It is noon."
    assert chat_client.chat_stream.call_count == 2
    registry.call.assert_called_once()


# ---- conversation title derivation and activity timestamp ----

def test_send_message_derives_a_title_from_the_first_message() -> None:
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("hi there")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    service = ChatService(db, chat_client=chat_client, retrieval_service=retrieval_service)
    service.send_message("  What does my Bangalore to Germany roadmap say?  ")

    conversations = [c for c in db.add.call_args_list if isinstance(c.args[0], Conversation)]
    assert conversations[0].args[0].title == "What does my Bangalore to Germany roadmap say?"


def test_send_message_truncates_a_long_first_message_for_the_title() -> None:
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("ok")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    long_message = "x" * 100
    service = ChatService(db, chat_client=chat_client, retrieval_service=retrieval_service)
    service.send_message(long_message)

    conversations = [c for c in db.add.call_args_list if isinstance(c.args[0], Conversation)]
    title = conversations[0].args[0].title
    assert title == "x" * 60 + "…"


def test_send_message_never_overwrites_an_existing_title() -> None:
    db = MagicMock()
    conversation = Conversation(id="conv-1", title="Existing title")
    db.get.return_value = conversation
    db.scalars.return_value = []
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("reply")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    service = ChatService(db, chat_client=chat_client, retrieval_service=retrieval_service)
    service.send_message("a completely different message", conversation_id="conv-1")

    assert conversation.title == "Existing title"


def test_send_message_touches_conversation_updated_at() -> None:
    from datetime import UTC, datetime, timedelta

    db = MagicMock()
    original_updated_at = datetime.now(UTC) - timedelta(hours=1)
    conversation = Conversation(id="conv-1", title="t", updated_at=original_updated_at)
    db.get.return_value = conversation
    db.scalars.return_value = []
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("reply")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    service = ChatService(db, chat_client=chat_client, retrieval_service=retrieval_service)
    service.send_message("hello", conversation_id="conv-1")

    assert conversation.updated_at > original_updated_at


# ---- chat attachments (files uploaded directly into a turn) ----

def test_send_message_includes_attachment_text_in_the_prompt() -> None:
    from app.models.chat_attachment import ChatAttachment

    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("Based on that file, ...")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []
    attachment_service = MagicMock()
    attachment_service.get_many.return_value = [
        ChatAttachment(id="att-1", original_filename="report.txt", stored_path="/x", byte_size=3,
                       extracted_text="quarterly numbers here", truncated=False)
    ]

    service = ChatService(
        db, chat_client=chat_client, retrieval_service=retrieval_service,
        attachment_service=attachment_service,
    )
    service.send_message("what does this say?", attachment_ids=["att-1"])

    attachment_service.get_many.assert_called_once_with(["att-1"])
    system_content = chat_client.chat.call_args.args[0][0]["content"]
    assert "Files the user attached to this message:" in system_content
    assert "report.txt" in system_content
    assert "quarterly numbers here" in system_content


def test_send_message_notes_when_an_attachment_was_truncated() -> None:
    from app.models.chat_attachment import ChatAttachment

    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("ok")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []
    attachment_service = MagicMock()
    attachment_service.get_many.return_value = [
        ChatAttachment(id="att-1", original_filename="big.txt", stored_path="/x", byte_size=999999,
                       extracted_text="only the first part", truncated=True)
    ]

    service = ChatService(
        db, chat_client=chat_client, retrieval_service=retrieval_service,
        attachment_service=attachment_service,
    )
    service.send_message("summarize", attachment_ids=["att-1"])

    system_content = chat_client.chat.call_args.args[0][0]["content"]
    assert "truncated" in system_content.lower()


def test_send_message_omits_attachments_block_when_none_given() -> None:
    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("hi")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []

    service = ChatService(db, chat_client=chat_client, retrieval_service=retrieval_service)
    service.send_message("hello")

    system_content = chat_client.chat.call_args.args[0][0]["content"]
    # the base system prompt always explains what an attachments block would
    # mean if present; only the block itself (with a real file) is optional
    assert "Files the user attached to this message:" not in system_content


def test_send_message_includes_web_results_when_enabled(monkeypatch) -> None:
    from app.core.config import settings
    from app.services.web_search_service import WebSearchResult

    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)

    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("Based on the web, ...")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []
    web_search_service = MagicMock()
    web_search_service.search.return_value = [
        WebSearchResult(title="Some Result", url="https://example.com", snippet="a snippet")
    ]

    service = ChatService(
        db, chat_client=chat_client, retrieval_service=retrieval_service,
        web_search_service=web_search_service,
    )
    service.send_message("what's new today?", web_search=True)

    web_search_service.search.assert_called_once_with("what's new today?")
    system_content = chat_client.chat.call_args.args[0][0]["content"]
    assert "Web search results for this message:" in system_content
    assert "[W1] Some Result (https://example.com)" in system_content
    assert "a snippet" in system_content


def test_send_message_skips_web_search_when_globally_disabled(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", False)

    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("hi")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []
    web_search_service = MagicMock()

    service = ChatService(
        db, chat_client=chat_client, retrieval_service=retrieval_service,
        web_search_service=web_search_service,
    )
    # web_search=True on the request, but the server-wide switch is off
    service.send_message("what's new today?", web_search=True)

    web_search_service.search.assert_not_called()
    system_content = chat_client.chat.call_args.args[0][0]["content"]
    assert "Web search results for this message:" not in system_content


def test_send_message_skips_web_search_when_not_requested(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)

    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("hi")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []
    web_search_service = MagicMock()

    service = ChatService(
        db, chat_client=chat_client, retrieval_service=retrieval_service,
        web_search_service=web_search_service,
    )
    service.send_message("hello")  # web_search defaults to False

    web_search_service.search.assert_not_called()


def test_send_message_tells_model_when_web_search_fails(monkeypatch) -> None:
    from app.core.config import settings
    from app.services.web_search_service import WebSearchUnavailableError

    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)

    db = MagicMock()
    db.get.return_value = None
    db.scalars.return_value = []
    db.refresh.side_effect = _make_fake_refresh()
    chat_client = MagicMock()
    chat_client.chat.return_value = _reply("hi")
    retrieval_service = MagicMock()
    retrieval_service.search.return_value = []
    web_search_service = MagicMock()
    web_search_service.search.side_effect = WebSearchUnavailableError("connection refused")

    service = ChatService(
        db, chat_client=chat_client, retrieval_service=retrieval_service,
        web_search_service=web_search_service,
    )
    service.send_message("what's new today?", web_search=True)

    system_content = chat_client.chat.call_args.args[0][0]["content"]
    assert "failed and returned no results" in system_content

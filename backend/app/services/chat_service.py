import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.memory.service import MemoryService
from app.models.chat_attachment import ChatAttachment
from app.models.conversation import Conversation
from app.models.memory import Memory, MemoryStatus
from app.models.message import Message, MessageRole
from app.models.tool_call import ToolCallRecord, ToolCallStatus
from app.rag.retrieval_service import RetrievalService, RetrievedChunk
from app.services.chat_attachment_service import ChatAttachmentService
from app.services.chat_client import ChatClient
from app.services.web_search_service import WebSearchResult, WebSearchService, WebSearchUnavailableError
from app.tools.builtin import build_default_registry
from app.tools.registry import ToolCallResult, ToolRegistry

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are AI_Brain, a personal offline assistant. When a numbered "
    "'Context from your documents' list is provided below, answer using "
    "it when relevant and cite it with the matching [n] marker; if it "
    "isn't relevant, say so and answer from general knowledge instead. "
    "A 'What you know about the user' list, if provided, is remembered "
    "facts about the user — state them plainly when relevant, with no "
    "[n] citation marker, since they aren't numbered. You also have "
    "tools available — use them when they'd give a better answer than "
    "the context already provided. Keep answers short and direct (a few "
    "sentences or a short list) unless the user asks for detail. If the "
    "context contains the user's own notes on the topic, use them instead "
    "of saying you have no access to them. A 'Files the user attached to "
    "this message' block, if provided, is a file they just uploaded for "
    "this one message — treat it as the most direct source for anything "
    "it's relevant to, separate from the indexed document context above. "
    "A 'Web search results' block, if provided, is live internet content "
    "fetched just now because the user turned web search on for this "
    "message — cite it with the matching [Wn] marker, separate from the "
    "[n] markers used for indexed documents."
)

MAX_HISTORY_MESSAGES = 20

# A message this short (in words) is treated as a follow-up such as "and the
# timeline?": the search then also uses the previous user message, because
# on its own it carries too little meaning. PROVISIONAL - chosen from the
# offline-chat evaluation (one short follow-up), not calibrated.
FOLLOW_UP_MAX_WORDS = 8
MAX_MEMORIES = 50
MAX_TOOL_ITERATIONS = 5
TOOL_LOOP_EXHAUSTED_REPLY = (
    "I wasn't able to finish that after several tool calls — "
    "could you try rephrasing?"
)

# Cap on how much of a tool's result text gets persisted in the audit
# record (ToolCallRecord.result). This bounds the tool_calls table, not
# the model's actual context — the full, untruncated result is always
# what gets fed back to the model; only the stored audit copy is capped.
MAX_TOOL_RESULT_LENGTH = 4000


class ChatService:
    def __init__(
        self,
        db: Session,
        chat_client: ChatClient | None = None,
        retrieval_service: RetrievalService | None = None,
        memory_service: MemoryService | None = None,
        tool_registry: ToolRegistry | None = None,
        attachment_service: ChatAttachmentService | None = None,
        web_search_service: WebSearchService | None = None,
    ):
        self.db = db
        self.chat_client = chat_client or ChatClient()
        self.retrieval_service = retrieval_service or RetrievalService(db)
        self.memory_service = memory_service or MemoryService(db)
        self.attachment_service = attachment_service or ChatAttachmentService(db)
        self.web_search_service = web_search_service or WebSearchService()
        # None means "use the real, per-turn registry" - see send_message.
        # A caller-supplied registry (tests, mainly) is used as-is and
        # never rebuilt, since it has no conversation-specific behavior
        # to wire up.
        self._tool_registry_override = tool_registry
        self.tool_registry = tool_registry or build_default_registry(db)

    def send_message(
        self,
        content: str,
        conversation_id: str | None = None,
        top_k: int = 5,
        attachment_ids: list[str] | None = None,
        web_search: bool = False,
    ) -> Message:
        conversation, retrieved, prompt, proposed_memory_ids = self._prepare_turn(
            content, conversation_id, top_k, attachment_ids, web_search
        )

        reply_text, tool_call_ids = self._run_tool_loop(prompt, conversation.id)

        return self._finish_turn(conversation, reply_text, retrieved, tool_call_ids, proposed_memory_ids)

    def send_message_stream(
        self,
        content: str,
        conversation_id: str | None = None,
        top_k: int = 5,
        attachment_ids: list[str] | None = None,
        web_search: bool = False,
    ):
        """Same turn as `send_message`, but yields events while the reply is
        being written so a reader sees text at once instead of waiting for
        the whole answer: {"type": "token", "text": ...} for each piece,
        {"type": "reset"} if text shown so far turns out to precede a tool
        call (it is not the final answer), and finally
        {"type": "done", "message": <the saved assistant Message>}."""
        conversation, retrieved, prompt, proposed_memory_ids = self._prepare_turn(
            content, conversation_id, top_k, attachment_ids, web_search
        )

        reply_text, tool_call_ids = yield from self._run_tool_loop_stream(prompt, conversation.id)

        message = self._finish_turn(conversation, reply_text, retrieved, tool_call_ids, proposed_memory_ids)
        yield {"type": "done", "message": message}

    def _prepare_turn(
        self,
        content: str,
        conversation_id: str | None,
        top_k: int,
        attachment_ids: list[str] | None = None,
        web_search: bool = False,
    ):
        conversation = self._get_or_create_conversation(conversation_id)

        user_message = Message(
            conversation_id=conversation.id,
            role=MessageRole.USER,
            content=content,
        )
        self.db.add(user_message)
        self.db.commit()
        self.db.refresh(user_message)

        proposed_memory_ids: list[int] = []
        if self._tool_registry_override is None:
            self.tool_registry = build_default_registry(
                self.db,
                conversation_id=conversation.id,
                on_memory_proposed=proposed_memory_ids.append,
            )

        self._ensure_title(conversation, content)

        history = self._load_history(conversation.id)
        retrieved = self._relevant_only(
            self.retrieval_service.search(
                self._search_query(content, history, user_message.id), top_k=top_k
            )
        )
        # Only APPROVED memories ever reach the model - a PENDING proposal
        # (from `remember`, awaiting review) must not influence answers
        # before a human has confirmed it.
        memories = self.memory_service.list_memories(
            limit=MAX_MEMORIES,
            status=MemoryStatus.APPROVED,
        )
        attachments = self.attachment_service.get_many(attachment_ids or [])
        web_results, web_search_error = self._maybe_web_search(content, web_search)
        prompt = self._build_prompt(history, retrieved, memories, attachments, web_results, web_search_error)

        return conversation, retrieved, prompt, proposed_memory_ids

    def _finish_turn(
        self,
        conversation: Conversation,
        reply_text: str,
        retrieved: list[RetrievedChunk],
        tool_call_ids: list[int],
        proposed_memory_ids: list[int],
    ) -> Message:
        citations = self._build_citations(retrieved)

        assistant_message = Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=reply_text,
            citations=citations,
        )

        try:
            self.db.add(assistant_message)
            self.db.commit()
            self.db.refresh(assistant_message)

        except Exception:
            self.db.rollback()
            raise

        if tool_call_ids:
            self._link_tool_calls_to_message(tool_call_ids, assistant_message.id)

        if proposed_memory_ids:
            self._link_memories_to_message(proposed_memory_ids, assistant_message.id)

        conversation.updated_at = datetime.now(UTC)
        self.db.add(conversation)
        self.db.commit()

        return assistant_message

    def _run_tool_loop(
        self,
        messages: list[dict],
        conversation_id: str,
    ) -> tuple[str, list[int]]:
        """Drive the chat/tool-call loop until the model gives a plain reply.

        Each iteration: send the conversation so far (plus any tool
        results already gathered) to the model. If it asks to call
        tools, execute them, persist an audit record for each (see
        ToolCallRecord), and feed the results back for the next
        iteration. Capped at MAX_TOOL_ITERATIONS so a model that keeps
        calling tools without ever answering can't loop forever.

        Returns the final reply text and the ids of any ToolCallRecord
        rows created, so the caller can link them to the assistant
        Message once it exists (the message doesn't exist yet at the
        point a tool call happens mid-loop).
        """
        working_messages = list(messages)
        tools_schema = self.tool_registry.to_ollama_schema()
        tool_call_record_ids: list[int] = []

        for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
            reply = self.chat_client.chat(working_messages, tools=tools_schema)

            if not reply.tool_calls:
                return reply.content or "", tool_call_record_ids

            self._execute_tool_calls(
                reply.tool_calls, reply.content or "", working_messages, conversation_id, iteration, tool_call_record_ids
            )

        logger.warning(
            "Tool loop hit MAX_TOOL_ITERATIONS (%d) without a final reply",
            MAX_TOOL_ITERATIONS,
        )
        return TOOL_LOOP_EXHAUSTED_REPLY, tool_call_record_ids

    def _execute_tool_calls(
        self,
        tool_calls,
        reply_content: str,
        working_messages: list[dict],
        conversation_id: str,
        iteration: int,
        tool_call_record_ids: list[int],
    ) -> None:
        """Run the model's requested tool calls: record the assistant turn,
        execute each tool, persist its audit record, and append each result
        to the running conversation for the next model call."""
        working_messages.append(
            {
                "role": "assistant",
                "content": reply_content,
                "tool_calls": [
                    {
                        "function": {
                            "name": call.function.name,
                            "arguments": dict(call.function.arguments),
                        }
                    }
                    for call in tool_calls
                ],
            }
        )

        for call_index, call in enumerate(tool_calls):
            arguments = dict(call.function.arguments)
            logger.info("Tool call: %s(%s)", call.function.name, arguments)

            result = self.tool_registry.call(call.function.name, arguments)

            record_id = self._record_tool_call(
                conversation_id=conversation_id,
                tool_name=call.function.name,
                iteration=iteration,
                call_index=call_index,
                arguments=arguments,
                result=result,
            )
            if record_id is not None:
                tool_call_record_ids.append(record_id)

            working_messages.append(
                {
                    "role": "tool",
                    "tool_name": call.function.name,
                    "content": result.content,
                }
            )

    def _run_tool_loop_stream(self, messages: list[dict], conversation_id: str):
        """`_run_tool_loop`, but the model's text is yielded piece by piece
        as it is produced. Returns (final reply text, tool call record ids)
        through the generator's return value."""
        working_messages = list(messages)
        tools_schema = self.tool_registry.to_ollama_schema()
        tool_call_record_ids: list[int] = []

        for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
            pieces: list[str] = []
            tool_calls: list = []

            for part in self.chat_client.chat_stream(working_messages, tools=tools_schema):
                if part.content:
                    pieces.append(part.content)
                    yield {"type": "token", "text": part.content}
                if part.tool_calls:
                    tool_calls.extend(part.tool_calls)

            if not tool_calls:
                return "".join(pieces), tool_call_record_ids

            if pieces:
                yield {"type": "reset"}

            self._execute_tool_calls(
                tool_calls, "".join(pieces), working_messages, conversation_id, iteration, tool_call_record_ids
            )

        logger.warning(
            "Tool loop hit MAX_TOOL_ITERATIONS (%d) without a final reply",
            MAX_TOOL_ITERATIONS,
        )
        return TOOL_LOOP_EXHAUSTED_REPLY, tool_call_record_ids

    def _record_tool_call(
        self,
        conversation_id: str,
        tool_name: str,
        iteration: int,
        call_index: int,
        arguments: dict,
        result: ToolCallResult,
    ) -> int | None:
        """Persist an audit record for one tool call. Best-effort: a
        failure here is logged and swallowed rather than breaking the
        chat turn — audit logging must never be why a user doesn't get
        an answer.
        """
        result_text, was_truncated = self._truncate_for_audit(result.content)

        record = ToolCallRecord(
            conversation_id=conversation_id,
            message_id=None,
            tool_name=tool_name,
            iteration=iteration,
            call_index=call_index,
            arguments=arguments,
            status=ToolCallStatus.ERROR if result.is_error else ToolCallStatus.SUCCESS,
            result=result_text,
            result_truncated=was_truncated,
            error_message=result.error,
            duration_ms=result.duration_ms,
        )

        try:
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)
            return record.id

        except Exception:
            self.db.rollback()
            logger.warning(
                "Failed to persist tool call audit record for '%s'",
                tool_name,
                exc_info=True,
            )
            return None

    def _link_tool_calls_to_message(
        self,
        tool_call_ids: list[int],
        message_id: int,
    ) -> None:
        try:
            self.db.query(ToolCallRecord).filter(
                ToolCallRecord.id.in_(tool_call_ids)
            ).update({"message_id": message_id}, synchronize_session=False)
            self.db.commit()

        except Exception:
            self.db.rollback()
            logger.warning(
                "Failed to link tool call records %s to message %s",
                tool_call_ids,
                message_id,
                exc_info=True,
            )

    def _link_memories_to_message(
        self,
        memory_ids: list[int],
        message_id: int,
    ) -> None:
        try:
            self.db.query(Memory).filter(Memory.id.in_(memory_ids)).update(
                {"message_id": message_id}, synchronize_session=False
            )
            self.db.commit()

        except Exception:
            self.db.rollback()
            logger.warning(
                "Failed to link proposed memories %s to message %s",
                memory_ids,
                message_id,
                exc_info=True,
            )

    @staticmethod
    def _truncate_for_audit(text: str) -> tuple[str, bool]:
        if len(text) <= MAX_TOOL_RESULT_LENGTH:
            return text, False
        return text[:MAX_TOOL_RESULT_LENGTH], True

    def _get_or_create_conversation(
        self,
        conversation_id: str | None,
    ) -> Conversation:
        if conversation_id is not None:
            conversation = self.db.get(Conversation, conversation_id)

            if conversation is None:
                raise ValueError(f"Conversation {conversation_id} not found")

            return conversation

        conversation = Conversation()

        self.db.add(conversation)
        self.db.commit()
        self.db.refresh(conversation)

        return conversation

    @staticmethod
    def _search_query(content: str, history: list[Message], current_message_id: int | None) -> str:
        """The text to search with: the message itself, or - for a very short
        follow-up - the previous user message followed by it."""
        if len(content.split()) > FOLLOW_UP_MAX_WORDS:
            return content

        for message in reversed(history):
            if message.role == MessageRole.USER and message.id != current_message_id:
                return f"{message.content} {content}"

        return content

    def _maybe_web_search(
        self, content: str, web_search: bool
    ) -> tuple[list[WebSearchResult], str | None]:
        """Runs a live web search only when both the global switch
        (settings.WEB_SEARCH_ENABLED) and this message's own opt-in
        (`web_search`) are true. A failed search never breaks the turn -
        the model is told it didn't get results instead of the request
        failing outright, same spirit as _link_tool_calls_to_message's
        best-effort audit writes."""
        if not (web_search and settings.WEB_SEARCH_ENABLED):
            return [], None

        try:
            return self.web_search_service.search(content), None
        except WebSearchUnavailableError as exc:
            logger.warning("Web search failed: %s", exc)
            return [], str(exc)

    @staticmethod
    def _relevant_only(retrieved: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Drop chunks farther than CHAT_MAX_SOURCE_DISTANCE (when set), so a
        greeting or a general-knowledge question does not carry five unrelated
        'sources'. Unset means keep everything (the previous behaviour)."""
        limit = settings.CHAT_MAX_SOURCE_DISTANCE
        if limit is None:
            return retrieved
        return [result for result in retrieved if result.distance <= limit]

    @staticmethod
    def _source_label(result: RetrievedChunk) -> str:
        """What the model (and the reader) should call this source: the
        original file's name when known, else the stored document title
        (a working copy is titled 'content.<ext>', which says nothing)."""
        occurrences = result.source_occurrences
        if occurrences:
            first = occurrences[0]
            path = (first.member_path or first.root_t7_path).replace("\\", "/")
            return path.rsplit("/", 1)[-1]
        return result.document.title

    @staticmethod
    def _ensure_title(conversation: Conversation, first_message: str) -> None:
        """A brand-new conversation has no title yet; derive one from the
        message that started it; a conversation that already has one is
        left alone (never overwritten by a later message)."""
        if conversation.title is not None:
            return
        cleaned = " ".join(first_message.split())
        conversation.title = cleaned[:60] + ("…" if len(cleaned) > 60 else "")

    def _load_history(self, conversation_id: str) -> list[Message]:
        statement = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        )
        messages = list(self.db.scalars(statement))

        return messages[-MAX_HISTORY_MESSAGES:]

    def _build_prompt(
        self,
        history: list[Message],
        retrieved: list[RetrievedChunk],
        memories: list[Memory],
        attachments: list[ChatAttachment] | None = None,
        web_results: list[WebSearchResult] | None = None,
        web_search_error: str | None = None,
    ) -> list[dict[str, str]]:
        context_block = self._format_context(retrieved)
        memory_block = self._format_memories(memories)
        attachments_block = self._format_attachments(attachments or [])
        web_results_block = self._format_web_results(web_results or [])

        system_content = SYSTEM_PROMPT
        if context_block:
            system_content += "\n\nContext from your documents:\n" + context_block
        else:
            system_content += "\n\nNo relevant documents were found for this query."

        if memory_block:
            system_content += "\n\nWhat you know about the user:\n" + memory_block

        if attachments_block:
            system_content += "\n\nFiles the user attached to this message:\n" + attachments_block

        if web_results_block:
            system_content += "\n\nWeb search results for this message:\n" + web_results_block
        elif web_search_error:
            system_content += (
                "\n\nThe user turned on web search for this message, but it "
                "failed and returned no results. Say so plainly if it's "
                "relevant, instead of answering as if you searched."
            )

        messages = [{"role": "system", "content": system_content}]
        messages.extend(
            {"role": message.role.value, "content": message.content}
            for message in history
        )

        return messages

    @staticmethod
    def _format_context(retrieved: list[RetrievedChunk]) -> str:
        return "\n\n".join(
            f"[{index}] {ChatService._source_label(result)}: {result.chunk.content}"
            for index, result in enumerate(retrieved, start=1)
        )

    @staticmethod
    def _format_memories(memories: list[Memory]) -> str:
        return "\n".join(f"- {memory.content}" for memory in memories)

    @staticmethod
    def _format_attachments(attachments: list[ChatAttachment]) -> str:
        blocks = []
        for a in attachments:
            note = " (truncated - this is not the whole file)" if a.truncated else ""
            blocks.append(f"--- {a.original_filename}{note} ---\n{a.extracted_text}")
        return "\n\n".join(blocks)

    @staticmethod
    def _format_web_results(results: list[WebSearchResult]) -> str:
        return "\n\n".join(
            f"[W{index}] {result.title} ({result.url})\n{result.snippet}"
            for index, result in enumerate(results, start=1)
        )

    @staticmethod
    def _build_citations(
        retrieved: list[RetrievedChunk],
    ) -> list[dict] | None:
        if not retrieved:
            return None

        return [
            {
                "document_chunk_id": result.chunk.id,
                "document_id": result.document.id,
                "document_title": result.document.title,
                "document_source": result.document.source,
                "source_occurrences": ChatService._occurrences_to_dicts(result.source_occurrences),
            }
            for result in retrieved
        ]

    @staticmethod
    def _occurrences_to_dicts(occurrences) -> list[dict] | None:
        """Maps the already-computed RetrievedChunk.source_occurrences
        (Milestone 22) into the same plain-dict shape this method
        already uses for the other citation fields - never a second,
        independent provenance query. `occurrences` is None for a
        Chain 1 result (no SourceInstance graph exists)."""
        if occurrences is None:
            return None
        return [
            {
                "root_t7_path": occurrence.root_t7_path,
                "member_path": occurrence.member_path,
                "archive_ancestry": (
                    [{"kind": step.kind, "path": step.path} for step in occurrence.archive_ancestry]
                    if occurrence.archive_ancestry is not None
                    else None
                ),
            }
            for occurrence in occurrences
        ]

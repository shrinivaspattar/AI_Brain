import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.service import MemoryService
from app.models.conversation import Conversation
from app.models.memory import Memory
from app.models.message import Message, MessageRole
from app.models.tool_call import ToolCallRecord, ToolCallStatus
from app.rag.retrieval_service import RetrievalService, RetrievedChunk
from app.services.chat_client import ChatClient
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
    "the context already provided."
)

MAX_HISTORY_MESSAGES = 20
MAX_MEMORIES = 50
MAX_TOOL_ITERATIONS = 5

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
    ):
        self.db = db
        self.chat_client = chat_client or ChatClient()
        self.retrieval_service = retrieval_service or RetrievalService(db)
        self.memory_service = memory_service or MemoryService(db)
        self.tool_registry = tool_registry or build_default_registry(db)

    def send_message(
        self,
        content: str,
        conversation_id: str | None = None,
        top_k: int = 5,
    ) -> Message:
        conversation = self._get_or_create_conversation(conversation_id)

        user_message = Message(
            conversation_id=conversation.id,
            role=MessageRole.USER,
            content=content,
        )
        self.db.add(user_message)
        self.db.commit()
        self.db.refresh(user_message)

        retrieved = self.retrieval_service.search(content, top_k=top_k)
        memories = self.memory_service.list_memories(limit=MAX_MEMORIES)
        history = self._load_history(conversation.id)
        prompt = self._build_prompt(history, retrieved, memories)

        reply_text, tool_call_ids = self._run_tool_loop(prompt, conversation.id)

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

            working_messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": call.function.name,
                                "arguments": dict(call.function.arguments),
                            }
                        }
                        for call in reply.tool_calls
                    ],
                }
            )

            for call_index, call in enumerate(reply.tool_calls):
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

        logger.warning(
            "Tool loop hit MAX_TOOL_ITERATIONS (%d) without a final reply",
            MAX_TOOL_ITERATIONS,
        )
        return (
            "I wasn't able to finish that after several tool calls — "
            "could you try rephrasing?"
        ), tool_call_record_ids

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
    ) -> list[dict[str, str]]:
        context_block = self._format_context(retrieved)
        memory_block = self._format_memories(memories)

        system_content = SYSTEM_PROMPT
        if context_block:
            system_content += "\n\nContext from your documents:\n" + context_block
        else:
            system_content += "\n\nNo relevant documents were found for this query."

        if memory_block:
            system_content += "\n\nWhat you know about the user:\n" + memory_block

        messages = [{"role": "system", "content": system_content}]
        messages.extend(
            {"role": message.role.value, "content": message.content}
            for message in history
        )

        return messages

    @staticmethod
    def _format_context(retrieved: list[RetrievedChunk]) -> str:
        return "\n\n".join(
            f"[{index}] {result.document.title}: {result.chunk.content}"
            for index, result in enumerate(retrieved, start=1)
        )

    @staticmethod
    def _format_memories(memories: list[Memory]) -> str:
        return "\n".join(f"- {memory.content}" for memory in memories)

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
            }
            for result in retrieved
        ]

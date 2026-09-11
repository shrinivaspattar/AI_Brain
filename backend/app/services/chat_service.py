from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.message import Message, MessageRole
from app.rag.retrieval_service import RetrievalService, RetrievedChunk
from app.services.chat_client import ChatClient

SYSTEM_PROMPT = (
    "You are AI_Brain, a personal offline assistant. Answer using the "
    "context below from the user's own documents when it's relevant, and "
    "cite sources using the [n] markers shown next to each context item. "
    "If the context isn't relevant, say so and answer from general "
    "knowledge instead."
)

MAX_HISTORY_MESSAGES = 20


class ChatService:
    def __init__(
        self,
        db: Session,
        chat_client: ChatClient | None = None,
        retrieval_service: RetrievalService | None = None,
    ):
        self.db = db
        self.chat_client = chat_client or ChatClient()
        self.retrieval_service = retrieval_service or RetrievalService(db)

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
        history = self._load_history(conversation.id)
        prompt = self._build_prompt(history, retrieved)

        reply = self.chat_client.chat(prompt)

        citations = self._build_citations(retrieved)

        assistant_message = Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=reply,
            citations=citations,
        )

        try:
            self.db.add(assistant_message)
            self.db.commit()
            self.db.refresh(assistant_message)
            return assistant_message

        except Exception:
            self.db.rollback()
            raise

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
    ) -> list[dict[str, str]]:
        context_block = self._format_context(retrieved)

        system_content = SYSTEM_PROMPT
        if context_block:
            system_content += "\n\nContext from your documents:\n" + context_block
        else:
            system_content += "\n\nNo relevant documents were found for this query."

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

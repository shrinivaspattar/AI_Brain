from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from sqlalchemy.orm import Session

from app.dedup.service import DeduplicationService
from app.memory.service import MemoryService
from app.rag.retrieval_service import RetrievalService
from app.services.document_service import DocumentService
from app.tools.registry import Tool, ToolRegistry


def build_default_registry(
    db: Session,
    conversation_id: str | None = None,
    on_memory_proposed: Callable[[int], None] | None = None,
) -> ToolRegistry:
    """Build the registry of tools available to the chat loop.

    Deliberately read-only over the user's data, with one narrow
    exception: `remember`, which proposes a memory but never writes it
    live (see below) - no filesystem access, no network calls beyond the
    local Ollama instance already used for chat/embeddings. Broader tools
    (file operations, external APIs) are a separate, explicit decision -
    not something to bundle in by default.

    `conversation_id` and `on_memory_proposed` exist for `remember`'s
    provenance/backfill: ChatService rebuilds this registry once per
    `send_message` call (when the caller hasn't supplied its own
    ToolRegistry) so `remember` can attach the current conversation and
    report back which Memory rows it created, for message_id backfill
    once the assistant Message exists.
    """
    registry = ToolRegistry()

    retrieval_service = RetrievalService(db)
    document_service = DocumentService(db)
    memory_service = MemoryService(db)
    dedup_service = DeduplicationService(db)

    def search_knowledge_base(query: str, top_k: int = 5) -> str:
        results = retrieval_service.search(query, top_k=int(top_k))

        if not results:
            return "No relevant documents found."

        return "\n\n".join(
            f"[{index}] {result.document.title}: {result.chunk.content}"
            for index, result in enumerate(results, start=1)
        )

    registry.register(
        Tool(
            name="search_knowledge_base",
            description=(
                "Search the user's ingested documents for information "
                "relevant to a query. Use this to look something up "
                "that isn't already covered by the conversation context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5).",
                    },
                },
                "required": ["query"],
            },
            handler=search_knowledge_base,
        )
    )

    def get_current_datetime() -> str:
        return datetime.now(UTC).isoformat()

    registry.register(
        Tool(
            name="get_current_datetime",
            description="Get the current date and time (UTC, ISO 8601).",
            parameters={"type": "object", "properties": {}},
            handler=get_current_datetime,
        )
    )

    def list_recent_documents(limit: int = 10) -> str:
        documents = document_service.list_documents(limit=int(limit))

        if not documents:
            return "No documents have been ingested yet."

        return "\n".join(
            f"{document.title} ({document.source_type}) - {document.source}"
            for document in documents
        )

    registry.register(
        Tool(
            name="list_recent_documents",
            description="List the most recently ingested documents.",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum documents to list (default 10).",
                    },
                },
            },
            handler=list_recent_documents,
        )
    )

    def remember(content: str, confidence: float | None = None) -> str:
        memory = memory_service.propose_memory(
            content=content,
            confidence=confidence,
            conversation_id=conversation_id,
        )

        if on_memory_proposed is not None:
            on_memory_proposed(memory.id)

        return (
            f'Noted: proposed "{content}" as a memory (pending review). '
            "It won't be used in future conversations until the user "
            "approves it."
        )

    def find_duplicate_documents(scope: str = "exact") -> str:
        if scope == "near":
            pairs = dedup_service.find_near_duplicate_documents()

            if not pairs:
                return "No near-duplicate documents found."

            return "\n".join(
                f"{pair.document_a.title} ~ {pair.document_b.title} "
                f"(similarity {pair.similarity:.2f})"
                for pair in pairs
            )

        groups = dedup_service.find_exact_duplicates()

        if not groups:
            return "No exact duplicate documents found."

        return "\n".join(
            f"{len(group.documents)} identical copies: "
            + ", ".join(document.title for document in group.documents)
            for group in groups
        )

    registry.register(
        Tool(
            name="find_duplicate_documents",
            description=(
                "Find duplicate ingested documents. 'exact' finds "
                "byte-identical files (same content hash); 'near' finds "
                "documents whose opening content is highly similar but "
                "not identical. This only reports duplicates - it never "
                "deletes or modifies anything."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["exact", "near"],
                        "description": "Which kind of duplicates to look for (default exact).",
                    },
                },
            },
            handler=find_duplicate_documents,
        )
    )

    registry.register(
        Tool(
            name="remember",
            description=(
                "Propose a fact or preference about the user to remember "
                "for future conversations. This does not take effect "
                "immediately - it is queued for the user to review and "
                "approve or reject, since you might be wrong. Only "
                "propose things the user actually told you or that are "
                "clearly and directly stated, not speculation or "
                "inference."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "The fact or preference to remember, stated "
                            "plainly and in third person (e.g. \"The "
                            "user's name is Alex.\")."
                        ),
                    },
                    "confidence": {
                        "type": "number",
                        "description": (
                            "How confident you are this is accurate, "
                            "from 0 to 1 (optional)."
                        ),
                    },
                },
                "required": ["content"],
            },
            handler=remember,
        )
    )

    return registry

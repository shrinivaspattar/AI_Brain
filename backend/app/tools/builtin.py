from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.rag.retrieval_service import RetrievalService
from app.services.document_service import DocumentService
from app.tools.registry import Tool, ToolRegistry


def build_default_registry(db: Session) -> ToolRegistry:
    """Build the registry of tools available to the chat loop.

    Deliberately read-only: no filesystem access, no network calls beyond
    the local Ollama instance already used for chat/embeddings, no writes.
    Broader tools (file operations, external APIs) are a separate,
    explicit decision - not something to bundle in by default.
    """
    registry = ToolRegistry()

    retrieval_service = RetrievalService(db)
    document_service = DocumentService(db)

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

    return registry

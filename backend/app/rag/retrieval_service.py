from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.embeddings.client import EmbeddingClient
from app.models.document import Document
from app.models.document_chunk import DocumentChunk


@dataclass(slots=True, frozen=True)
class RetrievedChunk:
    chunk: DocumentChunk
    document: Document
    distance: float


class RetrievalService:
    """Embeds a query and finds the nearest document chunks via pgvector."""

    def __init__(
        self,
        db: Session,
        embedding_client: EmbeddingClient | None = None,
    ):
        self.db = db
        self.embedding_client = embedding_client or EmbeddingClient()

    def search(
        self,
        query: str,
        top_k: int = 5,
    ) -> list[RetrievedChunk]:
        query = query.strip()

        if not query or top_k <= 0:
            return []

        query_embedding = self.embedding_client.embed([query])[0]

        distance = DocumentChunk.embedding.cosine_distance(query_embedding)

        statement = (
            select(DocumentChunk, Document, distance.label("distance"))
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(DocumentChunk.embedding.is_not(None))
            .order_by(distance)
            .limit(top_k)
        )

        rows = self.db.execute(statement).all()

        return [
            RetrievedChunk(chunk=chunk, document=document, distance=float(dist))
            for chunk, document, dist in rows
        ]

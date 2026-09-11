from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.embeddings.chunker import chunk_text
from app.embeddings.client import EmbeddingClient
from app.models.document import Document
from app.models.document_chunk import DocumentChunk


class EmbeddingService:
    """Chunks a document's text content and persists embedded chunks."""

    def __init__(
        self,
        db: Session,
        embedding_client: EmbeddingClient | None = None,
    ):
        self.db = db
        self.embedding_client = embedding_client or EmbeddingClient()

    def embed_document(
        self,
        document: Document,
        content: str,
    ) -> list[DocumentChunk]:
        """Replace a document's chunks with freshly embedded ones from `content`."""
        chunks = chunk_text(content)

        try:
            self.db.execute(
                delete(DocumentChunk).where(
                    DocumentChunk.document_id == document.id
                )
            )

            if not chunks:
                self.db.commit()
                return []

            embeddings = self.embedding_client.embed(chunks)

            records = [
                DocumentChunk(
                    document_id=document.id,
                    chunk_index=index,
                    content=chunk,
                    embedding=embedding,
                )
                for index, (chunk, embedding) in enumerate(zip(chunks, embeddings))
            ]

            self.db.add_all(records)
            self.db.commit()

            for record in records:
                self.db.refresh(record)

            return records

        except Exception:
            self.db.rollback()
            raise

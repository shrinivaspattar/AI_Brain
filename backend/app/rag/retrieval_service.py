from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.embeddings.cache import CachedEmbeddingClient, build_query_embedding_client
from app.embeddings.client import EmbeddingClient
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.provenance_link import ProvenanceLink
from app.rag.bm25_index import Bm25IndexCache, default_bm25_cache
from app.rag.fusion import rrf_fuse
from app.models.source_instance import SourceInstance


@dataclass(slots=True, frozen=True)
class AncestryStep:
    """One node of a SourceInstance's own archive-ancestry chain -
    mirrors ProvenanceLink(kind, path) verbatim, never an invented
    display format (Milestone 22 design)."""

    kind: str
    path: str


@dataclass(slots=True, frozen=True)
class SourceOccurrence:
    """One physical observed occurrence of a Chain 2 Document's content
    identity - never "the" source, never ranked by authority. Milestone
    22's frozen semantic: "this content was observed at these source
    locations," nothing more. `archive_ancestry` is populated only when
    the occurrence's ProvenanceLink chain is genuinely nested (more than
    one archive level) - `root_t7_path`/`member_path` alone already
    fully describe a loose file or a single-level archive member."""

    root_t7_path: str
    member_path: str | None
    archive_ancestry: list[AncestryStep] | None


@dataclass(slots=True, frozen=True)
class RetrievedChunk:
    chunk: DocumentChunk
    document: Document
    distance: float
    # None for a Chain 1 Document (no SourceInstance graph exists at
    # all - structurally absent, never fabricated); a
    # SourceInstance.id-ordered list of every occurrence for a Chain 2
    # Document, unfiltered by canonical_status (Milestone 22 design -
    # canonicality plays no role in this milestone).
    source_occurrences: list[SourceOccurrence] | None = None


class RetrievalService:
    """Embeds a query and finds the nearest document chunks via pgvector."""

    def __init__(
        self,
        db: Session,
        embedding_client: EmbeddingClient | CachedEmbeddingClient | None = None,
        bm25_cache: Bm25IndexCache | None = None,
    ):
        self.db = db
        self.embedding_client = embedding_client or build_query_embedding_client()
        self.bm25_cache = bm25_cache or default_bm25_cache

    def search(
        self,
        query: str,
        top_k: int = 5,
    ) -> list[RetrievedChunk]:
        query = query.strip()

        if not query or top_k <= 0:
            return []

        query_embedding = self.embedding_client.embed([query])[0]

        if settings.SEARCH_HYBRID_ENABLED:
            return self._hybrid_search(query, top_k, query_embedding)

        distance = DocumentChunk.embedding.cosine_distance(query_embedding)

        statement = (
            select(DocumentChunk, Document, distance.label("distance"))
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(DocumentChunk.embedding.is_not(None))
            .order_by(distance, DocumentChunk.id)
            .limit(top_k)
        )

        rows = self.db.execute(statement).all()

        return self._build_results(rows)

    def _hybrid_search(self, query: str, top_k: int, query_embedding: list[float]) -> list[RetrievedChunk]:
        """Dense (pgvector) and BM25 rankings fused with Reciprocal Rank
        Fusion. Each ranking contributes its top `SEARCH_HYBRID_POOL` chunks
        (never fewer than top_k). A chunk found only by BM25 still gets a real
        cosine distance, computed in the final fetch."""
        pool = max(settings.SEARCH_HYBRID_POOL, top_k)
        distance = DocumentChunk.embedding.cosine_distance(query_embedding)

        dense_ids = [
            row[0]
            for row in self.db.execute(
                select(DocumentChunk.id)
                .where(DocumentChunk.embedding.is_not(None))
                .order_by(distance, DocumentChunk.id)
                .limit(pool)
            )
        ]
        bm25_ids = self.bm25_cache.get(self.db).search(query, pool)

        fused = rrf_fuse([dense_ids, bm25_ids])[:top_k]
        if not fused:
            return []

        rows = self.db.execute(
            select(DocumentChunk, Document, distance.label("distance"))
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(DocumentChunk.id.in_(fused))
        ).all()
        by_id = {chunk.id: (chunk, document, dist) for chunk, document, dist in rows}
        return self._build_results([by_id[chunk_id] for chunk_id in fused if chunk_id in by_id])

    def _build_results(self, rows) -> list[RetrievedChunk]:
        occurrences_by_group = self._occurrences_by_content_identity_group(
            document.content_identity_group_id
            for _chunk, document, _dist in rows
            if document.content_identity_group_id is not None
        )

        return [
            RetrievedChunk(
                chunk=chunk,
                document=document,
                distance=float(dist),
                source_occurrences=occurrences_by_group.get(document.content_identity_group_id),
            )
            for chunk, document, dist in rows
        ]

    def _occurrences_by_content_identity_group(
        self, group_ids
    ) -> dict[int, list[SourceOccurrence]]:
        """Batched, O(1)-query provenance lookup (Milestone 22 design) -
        never one query per chunk/document/SourceInstance. Chain 1
        Documents (content_identity_group_id is None) never reach this
        method's caller, so they contribute nothing here by
        construction, not by an explicit filter."""
        distinct_group_ids = sorted(set(group_ids))
        if not distinct_group_ids:
            return {}

        instance_rows = self.db.execute(
            select(SourceInstance)
            .where(SourceInstance.content_identity_group_id.in_(distinct_group_ids))
            .order_by(SourceInstance.id)
        ).scalars().all()

        instance_ids = [instance.id for instance in instance_rows]
        links_by_instance: dict[int, list[ProvenanceLink]] = {}
        if instance_ids:
            link_rows = self.db.execute(
                select(ProvenanceLink)
                .where(ProvenanceLink.source_instance_id.in_(instance_ids))
                .order_by(ProvenanceLink.source_instance_id, ProvenanceLink.sequence_index)
            ).scalars().all()
            for link in link_rows:
                links_by_instance.setdefault(link.source_instance_id, []).append(link)

        occurrences_by_group: dict[int, list[SourceOccurrence]] = {}
        for instance in instance_rows:
            chain = links_by_instance.get(instance.id, [])
            # "Nested" means more than one archive level - a loose file
            # (chain length 1) or a single-level archive member (chain
            # length 2) is already fully described by root_t7_path/
            # member_path alone (Milestone 22 design, section 3/12).
            archive_ancestry = (
                [AncestryStep(kind=link.kind.value, path=link.path) for link in chain]
                if len(chain) > 2
                else None
            )
            occurrence = SourceOccurrence(
                root_t7_path=instance.root_t7_path,
                member_path=instance.member_path,
                archive_ancestry=archive_ancestry,
            )
            occurrences_by_group.setdefault(instance.content_identity_group_id, []).append(occurrence)

        return occurrences_by_group

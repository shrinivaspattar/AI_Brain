from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.models.document import Document
from app.models.document_chunk import DocumentChunk

DEFAULT_GROUP_LIMIT = 100
DEFAULT_NEAR_DUPLICATE_THRESHOLD = 0.95


@dataclass(frozen=True)
class ExactDuplicateGroup:
    content_hash: str
    documents: list[Document]


@dataclass(frozen=True)
class NearDuplicatePair:
    document_a: Document
    document_b: Document
    similarity: float


@dataclass(frozen=True)
class DryRunAction:
    action: str
    document: Document
    reason: str


@dataclass(frozen=True)
class ExactDuplicatePlan:
    content_hash: str
    keep: Document
    actions: list[DryRunAction]


class DeduplicationService:
    """Detection (and dry-run planning) only - this never deletes, moves,
    or quarantines a file, and never writes anything to the database.

    `plan_exact_duplicate_cleanup` performs Best Copy Arbitration and
    proposes a cleanup plan, but a plan is just data returned to the
    caller - producing one has zero effect on any file or row. Actually
    carrying out a plan is a separate, riskier concern (KRM's own "Dry-run
    mode" backlog item exists precisely because that step isn't built
    yet) and intentionally not part of this service.
    """

    def __init__(self, db: Session):
        self.db = db

    def find_exact_duplicates(
        self,
        limit: int = DEFAULT_GROUP_LIMIT,
    ) -> list[ExactDuplicateGroup]:
        """Group documents that share an identical content_hash.

        Exact-hash matching: zero false positives, but only catches
        byte-identical files - a re-saved or re-compressed copy won't
        match even if the content is effectively the same.
        """
        duplicate_hashes = (
            select(Document.content_hash)
            .where(Document.content_hash.is_not(None))
            .group_by(Document.content_hash)
            .having(func.count(Document.id) > 1)
            .limit(limit)
        )

        hashes = [row[0] for row in self.db.execute(duplicate_hashes).all()]

        if not hashes:
            return []

        documents = list(
            self.db.scalars(
                select(Document)
                .where(Document.content_hash.in_(hashes))
                .order_by(Document.content_hash, Document.created_at)
            )
        )

        groups: dict[str, list[Document]] = {}
        for document in documents:
            groups.setdefault(document.content_hash, []).append(document)

        return [
            ExactDuplicateGroup(content_hash=content_hash, documents=docs)
            for content_hash, docs in groups.items()
        ]

    def find_near_duplicate_documents(
        self,
        similarity_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
        limit: int = DEFAULT_GROUP_LIMIT,
    ) -> list[NearDuplicatePair]:
        """Find document pairs whose opening content is highly similar.

        Compares each document's first chunk (chunk_index=0) embedding
        against every other document's first chunk via pgvector cosine
        distance - a lightweight proxy for full-document similarity,
        not a true all-chunks comparison (which is O(chunk_count^2) and
        not needed for a first pass at personal-corpus scale). Two
        documents differing significantly beyond their opening ~1000
        characters won't be caught by this; revisit with a fuller
        comparison (e.g. an averaged per-document embedding) if that
        turns out to matter in practice.
        """
        if not 0 < similarity_threshold <= 1:
            raise ValueError("similarity_threshold must be in (0, 1]")

        distance_threshold = 1 - similarity_threshold

        chunk_a = aliased(DocumentChunk)
        chunk_b = aliased(DocumentChunk)

        distance = chunk_a.embedding.cosine_distance(chunk_b.embedding)

        statement = (
            select(chunk_a.document_id, chunk_b.document_id, distance.label("distance"))
            .where(chunk_a.chunk_index == 0)
            .where(chunk_b.chunk_index == 0)
            .where(chunk_a.embedding.is_not(None))
            .where(chunk_b.embedding.is_not(None))
            .where(chunk_a.document_id < chunk_b.document_id)
            .where(distance < distance_threshold)
            .order_by(distance)
            .limit(limit)
        )

        rows = self.db.execute(statement).all()

        if not rows:
            return []

        document_ids = {row[0] for row in rows} | {row[1] for row in rows}
        documents = {
            document.id: document
            for document in self.db.scalars(
                select(Document).where(Document.id.in_(document_ids))
            )
        }

        return [
            NearDuplicatePair(
                document_a=documents[document_a_id],
                document_b=documents[document_b_id],
                similarity=1 - float(dist),
            )
            for document_a_id, document_b_id, dist in rows
            if document_a_id in documents and document_b_id in documents
        ]

    def plan_exact_duplicate_cleanup(
        self,
        limit: int = DEFAULT_GROUP_LIMIT,
    ) -> list[ExactDuplicatePlan]:
        """Dry-run only: propose which copy to keep in each exact-duplicate
        group and which copies would be deleted, without deleting anything.

        Scoped to exact duplicates only - a byte-identical match is safe
        to arbitrate automatically. Near-duplicates are NOT included here:
        "highly similar" isn't "safe to discard," and arbitrating those
        needs human judgment, not a heuristic.

        Arbitration rule: keep the oldest copy (earliest `created_at`,
        `id` as a tiebreak for equal timestamps) and propose deleting the
        rest. This is a simple, deterministic first pass - it assumes the
        first-ingested copy is as good a "original" as any; it does not
        inspect file location, path depth, or filename quality. Revisit
        if that assumption turns out to be wrong in practice.
        """
        groups = self.find_exact_duplicates(limit=limit)

        plans = []
        for group in groups:
            ordered = sorted(group.documents, key=lambda d: (d.created_at, d.id))
            keep, *rest = ordered

            actions = [
                DryRunAction(
                    action="delete",
                    document=document,
                    reason=(
                        f"identical to kept copy '{keep.title}' "
                        f"(content_hash match); ingested {document.created_at} "
                        f"vs. kept copy's {keep.created_at}"
                    ),
                )
                for document in rest
            ]

            plans.append(
                ExactDuplicatePlan(
                    content_hash=group.content_hash,
                    keep=keep,
                    actions=actions,
                )
            )

        return plans

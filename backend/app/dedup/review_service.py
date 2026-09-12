from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dedup.service import ExactDuplicateGroup, NearDuplicatePair
from app.models.dedup_review import (
    DuplicateMatchType,
    DuplicateReview,
    DuplicateReviewMember,
    DuplicateReviewMemberRole,
    DuplicateReviewStatus,
)
from app.models.document import Document

DEFAULT_LIST_LIMIT = 100


def _member_evidence(document: Document) -> dict:
    """Point-in-time snapshot of the fields relevant to a dedup decision.

    Deliberately limited to what Document actually stores today -
    title/source/source_type/content_hash/import_job_id/created_at.
    No file size, filesystem mtime, or version metadata: none of that
    is captured anywhere in the ingestion pipeline, so it cannot be used
    as evidence without inventing data the system doesn't actually have.
    `created_at` is this row's ingestion time, not the file's filesystem
    timestamp - a caveat worth keeping in mind when a reviewer reads it.
    """
    return {
        "document_id": document.id,
        "title": document.title,
        "source": document.source,
        "source_type": document.source_type,
        "content_hash": document.content_hash,
        "import_job_id": document.import_job_id,
        "created_at": document.created_at.isoformat(),
    }


class DedupReviewService:
    """The DECISION layer of KRM deduplication - persisted, human-reviewable
    findings, deliberately separate from both DETECTION
    (DeduplicationService, stateless) and EXECUTION (does not exist
    anywhere in this codebase).

    Creating a review never touches a file or deletes/moves anything.
    Approving or rejecting a review never touches a file. There is no
    method here, or anywhere else in AI_Brain, that turns a review's
    outcome into a filesystem action.
    """

    def __init__(self, db: Session):
        self.db = db

    def create_review_from_exact_group(
        self,
        group: ExactDuplicateGroup,
    ) -> DuplicateReview:
        """Materialize a detected exact-duplicate group into a reviewable
        finding, proposing a canonical copy.

        Byte-identical content makes "which copy to keep" a safe,
        deterministic choice (not a content-quality judgment) - the same
        arbitration rule as `DeduplicationService.plan_exact_duplicate_cleanup`:
        keep the oldest-ingested copy, tie-broken by id. `confidence` is
        always 1.0 here: the *match* is proven by hash equality, not a
        heuristic - only the canonical *choice* is a heuristic, and it is
        always presented as a proposal a human can override, never a
        decision already made.
        """
        if len(group.documents) < 2:
            raise ValueError(
                "An exact-duplicate group must contain at least two documents"
            )

        ordered = sorted(group.documents, key=lambda d: (d.created_at, d.id))
        canonical, *duplicates = ordered

        review = DuplicateReview(
            match_type=DuplicateMatchType.EXACT,
            content_hash=group.content_hash,
            similarity=None,
            confidence=1.0,
            recommendation_reason=(
                f"All {len(ordered)} documents share identical content_hash "
                f"{group.content_hash} (byte-for-byte identical). Proposing "
                f"'{canonical.title}' as the canonical copy because it was "
                f"ingested first ({canonical.created_at.isoformat()}); the "
                "rest are proposed as redundant copies. This is a "
                "deterministic tiebreak, not a judgment about which file is "
                "'better' - a human reviewer may choose differently."
            ),
            evidence={
                "match_type": "exact",
                "content_hash": group.content_hash,
                "arbitration_rule": "oldest created_at, tie-broken by id",
                "members": [_member_evidence(d) for d in ordered],
            },
            status=DuplicateReviewStatus.PENDING,
        )
        self.db.add(review)
        self.db.flush()

        self.db.add(
            DuplicateReviewMember(
                review_id=review.id,
                document_id=canonical.id,
                role=DuplicateReviewMemberRole.RECOMMENDED_CANONICAL,
            )
        )
        for document in duplicates:
            self.db.add(
                DuplicateReviewMember(
                    review_id=review.id,
                    document_id=document.id,
                    role=DuplicateReviewMemberRole.DUPLICATE,
                )
            )

        try:
            self.db.commit()
            self.db.refresh(review)
            return review
        except Exception:
            self.db.rollback()
            raise

    def create_review_from_near_pair(
        self,
        pair: NearDuplicatePair,
    ) -> DuplicateReview:
        """Materialize a detected near-duplicate pair into a reviewable
        finding, WITHOUT proposing a canonical copy.

        This is deliberate, not an oversight: similarity alone is not
        evidence of which copy is more complete, correct, or current.
        Guessing based on size, recency, or ingestion order would be
        exactly the unsafe assumption this design explicitly avoids -
        so no `RECOMMENDED_CANONICAL` member is ever created here.
        Every near-duplicate review is, by construction, the "ambiguous"
        case: a human must decide whether these are really duplicates
        at all, and if so, which (if either) to prefer.

        `confidence` reflects confidence that this IS a genuine
        near-duplicate match (equal to the detected similarity score) -
        not confidence in a canonical choice, since none is made.
        """
        review = DuplicateReview(
            match_type=DuplicateMatchType.NEAR,
            content_hash=None,
            similarity=pair.similarity,
            confidence=pair.similarity,
            recommendation_reason=(
                f"'{pair.document_a.title}' and '{pair.document_b.title}' "
                f"have {pair.similarity:.0%} similar opening content but are "
                "not byte-identical. No canonical copy is proposed: "
                "deciding which (if either) to keep requires judgment this "
                "system cannot safely automate from similarity alone."
            ),
            evidence={
                "match_type": "near",
                "similarity": pair.similarity,
                "compared_chunk_index": 0,
                "members": [
                    _member_evidence(pair.document_a),
                    _member_evidence(pair.document_b),
                ],
                "canonical_note": (
                    "No automatic canonical selection for near-duplicates."
                ),
            },
            status=DuplicateReviewStatus.PENDING,
        )
        self.db.add(review)
        self.db.flush()

        self.db.add(
            DuplicateReviewMember(
                review_id=review.id,
                document_id=pair.document_a.id,
                role=DuplicateReviewMemberRole.DUPLICATE,
            )
        )
        self.db.add(
            DuplicateReviewMember(
                review_id=review.id,
                document_id=pair.document_b.id,
                role=DuplicateReviewMemberRole.DUPLICATE,
            )
        )

        try:
            self.db.commit()
            self.db.refresh(review)
            return review
        except Exception:
            self.db.rollback()
            raise

    def get_review(self, review_id: int) -> DuplicateReview | None:
        return self.db.get(DuplicateReview, review_id)

    def get_review_members_with_documents(
        self,
        review_id: int,
    ) -> list[tuple[DuplicateReviewMember, Document]]:
        """Each member of a review paired with its live Document row.

        A separate query rather than an ORM relationship traversal,
        matching this codebase's existing convention (see
        ProvenanceService.trace_document) of joining explicitly in the
        service layer instead of relying on relationship() for anything
        beyond DuplicateReview's own parent/child link.
        """
        members = list(
            self.db.scalars(
                select(DuplicateReviewMember).where(
                    DuplicateReviewMember.review_id == review_id
                )
            )
        )

        if not members:
            return []

        document_ids = {member.document_id for member in members}
        documents = {
            document.id: document
            for document in self.db.scalars(
                select(Document).where(Document.id.in_(document_ids))
            )
        }

        return [
            (member, documents[member.document_id])
            for member in members
            if member.document_id in documents
        ]

    def list_reviews(
        self,
        status: DuplicateReviewStatus | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[DuplicateReview]:
        statement = (
            select(DuplicateReview)
            .order_by(DuplicateReview.created_at.desc())
            .limit(limit)
        )

        if status is not None:
            statement = statement.where(DuplicateReview.status == status)

        return list(self.db.scalars(statement))

    def approve_review(
        self,
        review_id: int,
        canonical_document_id: str | None = None,
        reviewer_decision: str | None = None,
    ) -> DuplicateReview:
        """Record a human's approval. This has no effect beyond the
        review row itself - no file is touched, and no execution is
        triggered, because no execution mechanism exists yet.

        `canonical_document_id` is the human's explicit decision - it is
        never inferred from the review's RECOMMENDED_CANONICAL member,
        even when they happen to agree. Required for an EXACT review
        (approving an exact-duplicate finding without saying which copy
        to keep isn't a complete decision); optional for a NEAR review,
        where "confirmed as related, no canonical chosen" is itself a
        valid, deliberate outcome - never filled in automatically.

        Raises ValueError (mapped to 409 Conflict at the API layer) if
        the review has already been decided: unlike Memory's review
        gate, which permits re-deciding an already-reviewed row,
        dedup review deliberately enforces a one-way PENDING -> decided
        transition, since this is the stage that will eventually gate a
        real filesystem action and "do not silently allow inconsistent
        decisions" was an explicit design requirement here.
        """
        review = self._get_review_or_raise(review_id)
        self._require_pending(review)

        member_document_ids = self._require_members(review)

        if canonical_document_id is not None:
            self._validate_canonical_membership(
                review_id, canonical_document_id, member_document_ids
            )
        elif review.match_type == DuplicateMatchType.EXACT:
            raise ValueError(
                f"Duplicate review {review_id} is an exact-duplicate finding "
                "and requires an explicit canonical_document_id to approve"
            )

        try:
            review.status = DuplicateReviewStatus.APPROVED
            review.human_selected_canonical_document_id = canonical_document_id
            review.reviewer_decision = reviewer_decision
            review.reviewed_at = datetime.now(UTC)
            self.db.commit()
            self.db.refresh(review)
            return review
        except Exception:
            self.db.rollback()
            raise

    def reject_review(
        self,
        review_id: int,
        reviewer_decision: str | None = None,
    ) -> DuplicateReview:
        """Record a human's rejection - these are not duplicates that
        should be acted on. Raises ValueError (409) if the review has
        already been decided, same as approve_review."""
        review = self._get_review_or_raise(review_id)
        self._require_pending(review)

        try:
            review.status = DuplicateReviewStatus.REJECTED
            review.reviewer_decision = reviewer_decision
            review.reviewed_at = datetime.now(UTC)
            self.db.commit()
            self.db.refresh(review)
            return review
        except Exception:
            self.db.rollback()
            raise

    def _get_review_or_raise(self, review_id: int) -> DuplicateReview:
        review = self.get_review(review_id)

        if review is None:
            raise ValueError(f"Duplicate review {review_id} not found")

        return review

    def _require_pending(self, review: DuplicateReview) -> None:
        if review.status != DuplicateReviewStatus.PENDING:
            raise ValueError(
                f"Duplicate review {review.id} has already been reviewed "
                f"(status={review.status.value}) - it cannot be reviewed again"
            )

    def _require_members(self, review: DuplicateReview) -> set[str]:
        member_document_ids = {
            member.document_id
            for member in self.db.scalars(
                select(DuplicateReviewMember).where(
                    DuplicateReviewMember.review_id == review.id
                )
            )
        }

        if not member_document_ids:
            raise ValueError(
                f"Duplicate review {review.id} has no members and cannot be "
                "reviewed"
            )

        return member_document_ids

    def _validate_canonical_membership(
        self,
        review_id: int,
        canonical_document_id: str,
        member_document_ids: set[str],
    ) -> None:
        if canonical_document_id not in member_document_ids:
            raise ValueError(
                f"'{canonical_document_id}' is not a member of duplicate "
                f"review {review_id} - cannot select it as the canonical copy"
            )

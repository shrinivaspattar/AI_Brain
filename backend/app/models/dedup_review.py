from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class DuplicateMatchType(str, Enum):
    # Byte-identical (Document.content_hash equality). Zero false
    # positives - a safe, deterministic canonical choice is possible.
    EXACT = "exact"
    # Similar opening content (pgvector cosine similarity), not
    # byte-identical. Never gets an automatic canonical proposal - see
    # DuplicateReviewMemberRole.
    NEAR = "near"


class DuplicateReviewStatus(str, Enum):
    # Detected and recorded, awaiting a human decision. Carries zero
    # authority to act - a PENDING review is inert.
    PENDING = "pending"
    # A human reviewed this finding and agreed with (or overrode)
    # the system's recommendation. Still not an instruction to execute
    # anything - no execution mechanism exists yet (see DedupReviewService).
    APPROVED = "approved"
    # A human reviewed this finding and disagreed with it - these are
    # not duplicates that should be acted on. Kept, not deleted, as a
    # record of what was proposed and rejected (mirrors Memory.status).
    REJECTED = "rejected"


class DuplicateReviewMemberRole(str, Enum):
    # The system's RECOMMENDED best copy to keep - a suggestion only,
    # never a decision. Only ever assigned for EXACT reviews, where
    # byte-equality makes "which copy" an arbitrary but safe choice
    # (oldest ingested). NEVER assigned for NEAR reviews - similarity
    # alone is not evidence of which copy is more complete, correct, or
    # current, so no canonical is recommended; a review with no
    # RECOMMENDED_CANONICAL member is the explicit "ambiguous, needs
    # human judgment" case, not a missing/broken recommendation.
    #
    # This is deliberately a *different* concept from
    # DuplicateReview.human_selected_canonical_document_id, which is
    # never set by the system - only by an explicit human decision at
    # approval time (see DedupReviewService.approve_review). A
    # RECOMMENDED_CANONICAL member existing does NOT mean a canonical
    # has been decided; only human_selected_canonical_document_id being
    # non-null means that.
    RECOMMENDED_CANONICAL = "recommended_canonical"
    # Every other document in this finding - the copy/copies being
    # evaluated against the (possibly absent) recommended canonical.
    DUPLICATE = "duplicate"


class DuplicateReview(Base):
    """A persisted, human-reviewable finding from KRM deduplication.

    This is the DECISION layer, deliberately separate from both:
    - DETECTION (DeduplicationService.find_exact_duplicates /
      find_near_duplicate_documents - stateless, re-run on demand,
      produces no rows here on its own)
    - EXECUTION (does not exist anywhere in this codebase - approving a
      review has zero effect on any file; it only records a decision)

    Creating a review never touches a file. Approving or rejecting a
    review never touches a file. There is currently no code path
    anywhere that turns a review's outcome into a filesystem action -
    that is a deliberately separate, not-yet-built later stage.

    Recommended vs. approved canonical - kept explicitly distinct, never
    conflated: a `RECOMMENDED_CANONICAL` member (see
    DuplicateReviewMemberRole) is only ever the system's suggestion,
    computed once at review-creation time. `human_selected_canonical_document_id`
    is the only field that represents an actual decision, and it is set
    by nothing but an explicit human choice passed into
    `DedupReviewService.approve_review` - the system never copies its
    own recommendation into it automatically, even when a human simply
    agrees with the recommendation.
    """

    __tablename__ = "duplicate_reviews"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    match_type: Mapped[DuplicateMatchType] = mapped_column(
        SQLEnum(DuplicateMatchType, name="duplicate_match_type"),
        nullable=False,
    )

    # Set for EXACT reviews (the shared Document.content_hash); null for
    # NEAR reviews, which have no single shared hash to point to.
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    # Set for NEAR reviews (the pgvector cosine similarity, 0-1); null
    # for EXACT reviews, where hash equality is binary, not a score.
    similarity: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    # 0-1: how confident the system is that this finding IS a genuine
    # duplicate/near-duplicate match. For EXACT this is always 1.0
    # (hash equality is proof). For NEAR this equals `similarity`. This
    # is NOT confidence in a canonical choice - a NEAR review can be
    # highly confident these are related documents while still, by
    # design, proposing no canonical at all.
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    # Human-readable explanation of the recommendation (or, for NEAR,
    # the explicit absence of one). This is what makes the finding
    # explainable rather than an opaque score.
    recommendation_reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Structured, point-in-time snapshot of the evidence that produced
    # this recommendation: which documents, their content_hash/
    # similarity, timestamps, titles, sources, import_job_id. Snapshotted
    # (like Message.citations) rather than only a live join, so a review
    # still shows what evidence led to it even if a member Document is
    # later re-ingested or removed.
    evidence: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )

    status: Mapped[DuplicateReviewStatus] = mapped_column(
        SQLEnum(DuplicateReviewStatus, name="duplicate_review_status"),
        nullable=False,
        default=DuplicateReviewStatus.PENDING,
        server_default=DuplicateReviewStatus.PENDING.name,
    )

    # Optional free-text note from the human reviewer - distinct from
    # `recommendation_reason` (the system's explanation): this is the
    # human's own reasoning, especially useful when overriding the
    # system's recommendation or resolving an ambiguous (NEAR) finding.
    reviewer_decision: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # The human-approved canonical - set ONLY by an explicit choice
    # passed to approve_review(), never inferred from
    # RECOMMENDED_CANONICAL or any other heuristic. Null whenever a
    # review is PENDING, REJECTED, or was APPROVED without a canonical
    # being chosen (a valid, deliberate outcome for a NEAR review - see
    # DedupReviewService.approve_review).
    human_selected_canonical_document_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("documents.id"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    members: Mapped[list["DuplicateReviewMember"]] = relationship(
        back_populates="review",
        cascade="all, delete-orphan",
    )


class DuplicateReviewMember(Base):
    """One Document's role within a DuplicateReview finding.

    A child table rather than fixed columns on DuplicateReview because
    an EXACT group is genuinely N-way (DeduplicationService.
    find_exact_duplicates already supports more than 2 documents sharing
    a hash) - fixed `document_a_id`/`document_b_id` columns would silently
    truncate a real 3+-way duplicate group to a pair.
    """

    __tablename__ = "duplicate_review_members"
    __table_args__ = (
        UniqueConstraint(
            "review_id",
            "document_id",
            name="uq_duplicate_review_members_review_id_document_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    review_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("duplicate_reviews.id"),
        nullable=False,
        index=True,
    )

    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id"),
        nullable=False,
        index=True,
    )

    role: Mapped[DuplicateReviewMemberRole] = mapped_column(
        SQLEnum(DuplicateReviewMemberRole, name="duplicate_review_member_role"),
        nullable=False,
    )

    review: Mapped["DuplicateReview"] = relationship(back_populates="members")

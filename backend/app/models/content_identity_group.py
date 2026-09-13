from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ContentIdentityKind(str, Enum):
    # This milestone populates EXTRACTED_CONTENT exclusively (matching
    # the "always the extracted document content hash, never an
    # enclosing archive's source-file hash" rule). SOURCE_BYTES and
    # NORMALIZED_CONTENT exist so the identity namespace is unambiguous
    # if a future milestone ever needs them - not because this design
    # proposes populating them now.
    SOURCE_BYTES = "source_bytes"
    EXTRACTED_CONTENT = "extracted_content"
    NORMALIZED_CONTENT = "normalized_content"


class ContentIdentityAlgorithm(str, Enum):
    # Named explicitly rather than assumed forever - today's D1/ingestion
    # hashing is exclusively SHA-256, but the algorithm is a stated fact
    # of this row, not an implicit default.
    SHA256 = "sha256"


class ContentPipelineState(str, Enum):
    DISCOVERED = "discovered"
    CLASSIFIED = "classified"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    NORMALIZED = "normalized"
    CHUNKED = "chunked"
    EMBEDDED = "embedded"
    INGESTED = "ingested"
    # Non-processing outcomes - NOT a shared "quarantine" catch-all
    # (that word is reserved for the dedup executor's reversible
    # filesystem relocation, an unrelated concept). Each of these means
    # something categorically different:
    NEEDS_REVIEW = "needs_review"  # D2-flagged, blocked pending a human decision
    UNSUPPORTED = "unsupported"  # format/type not currently handled - never attempted
    EXCLUDED = "excluded"  # deliberately out of scope by policy (e.g. .c9r ciphertext) - NOT a failure
    FAILED = "failed"  # an attempted step genuinely errored


class ContentIdentityGroup(Base):
    """The content-identity equivalence class (layer 2 of this design's
    four identity layers) proven by a matching hash - NEVER inferred
    from a D1 directory-structural match, which is a different,
    weaker evidence class kept structurally separate (see SourceInstance).

    Canonicality (which physical SourceInstance is authoritative) is
    NOT decided here and is never inferred from this group's existence
    or pipeline_state - it lives on SourceInstance, scoped to its
    relationship with this group. This group only ever answers "is this
    content identity known, and how far has ITS pipeline progressed" -
    a single answer per group, which is the ingestion idempotency
    boundary: the extraction/normalization/chunking/embedding pipeline
    runs at most once per group, never once per SourceInstance.

    The identity domain is explicit (`identity_kind` + `identity_algorithm`
    + `identity_hash`) so two different hash layers can never be
    silently compared as if they were the same namespace, even though
    this milestone only ever populates one combination.

    WORKER CLAIM/LEASE FIELDS (added per the "Controlled T7 -> AI_Brain
    Ingestion Design", `6491dad`, round 2, point 1 - schema-extension
    gate): `claimed_by`/`claimed_at` are a transient, in-flight marker,
    NOT a permanent "who last touched this" record - both are cleared
    the moment an attempt finishes, success or failure. Permanent
    per-attempt history (who, when, what stage, outcome) lives in
    `IngestionAttempt` instead (see that model), never here. A claim is
    considered stale (reclaimable by any worker, including a different
    one than originally claimed it) once `claimed_at` is older than
    whatever lease duration the claiming query uses - no separate
    "recovery service" exists or is needed: the same claim query that
    grants fresh work also reclaims stale work, by construction (see
    `app.classification.worker_claim_service`).

    Because the frozen `pipeline_state` enum has an explicit in-progress
    marker for exactly one step (`EXTRACTING`) but none for
    normalization/chunking/embedding, this design uses `claimed_by`/
    `claimed_at` as the UNIFORM in-progress signal across every step:
    for extraction, claiming ALSO advances `pipeline_state` to
    `EXTRACTING` in the same atomic statement (reusing the existing
    enum value as intended); for every other step, `pipeline_state`
    stays at the previous step's completed value (e.g. `EXTRACTED`)
    for the entire duration a claim is held, and only advances on
    success. This asymmetry is inherited from the already-frozen enum,
    not introduced by this schema-extension gate - documented here so
    it is never mistaken for an oversight.

    GENERATION-FENCED RESERVATION PRIMITIVES (added per "Scaled Real-T7
    Ingestion - Implementation Design Pass", `2fab4b3`, Rounds 2-3;
    wired into a real lifecycle by Implementation Milestone 4):
    `claim_generation` increments by exactly 1 every time
    `WorkerClaimService.claim_content_identity_group` grants a claim on
    this row - whether that claim is fresh or a stale-reclaim. A caller
    holds the generation value returned by its own claim for the
    lifetime of its attempt. `reserved_embeddings` records how many
    embeddings a claim has reserved against this row's batch-level
    budget. Every mutating reservation operation (reserve/consume/
    release/recover) fences on `claim_generation = :my_generation` -
    closing a real ABA/zombie-worker race where a delayed (not merely
    crashed) worker could otherwise mistake a later generation's live
    reservation for its own stale one. Recovery is authorized because it
    reads this row fresh, under the same `SELECT ... FOR UPDATE` lock the
    claim query already takes - never because of a special-cased caller
    identity.

    `reserved_embeddings_batch_id` (added by Milestone 4's final
    correction, "Durable Reservation Ownership") records WHICH
    `IngestionBatch` a live reservation belongs to - closing a gap the
    original two-column design left open: without it, stale-claim
    recovery had no way to know which batch's `embeddings_reserved`
    counter to credit back when reclaiming an abandoned reservation
    (`ContentIdentityGroup` can legitimately be referenced by
    `SourceInstance` rows from many different batches, so the owning
    batch is never inferable from the group alone - see the "Why
    cross-batch identity convergence cannot create ownership ambiguity"
    reasoning in the frozen "### 8. Embedding reservation" design,
    which correctly keeps the CLAIM itself batch-agnostic but did not
    anticipate this specific recovery-time lookup need). FROZEN
    INVARIANT, enforced by a CHECK constraint below: `reserved_embeddings
    IS NULL` if and only if `reserved_embeddings_batch_id IS NULL` -
    never one set without the other. When a reservation is live:
    `reserved_embeddings > 0` (also CHECK-enforced) and
    `reserved_embeddings_batch_id` names the exact `IngestionBatch` that
    reserved it; `claim_generation` remains the fencing token for WHO
    (which attempt) may mutate the reservation, while
    `reserved_embeddings_batch_id` records WHAT (which batch's counter)
    that mutation must reconcile against - two orthogonal facts, never
    conflated.
    """

    __tablename__ = "content_identity_groups"
    __table_args__ = (
        UniqueConstraint(
            "identity_kind",
            "identity_algorithm",
            "identity_hash",
            name="uq_content_identity_groups_identity",
        ),
        CheckConstraint(
            "(reserved_embeddings IS NULL AND reserved_embeddings_batch_id IS NULL) "
            "OR (reserved_embeddings IS NOT NULL AND reserved_embeddings_batch_id IS NOT NULL)",
            name="ck_content_identity_groups_reservation_ownership_consistent",
        ),
        CheckConstraint(
            "reserved_embeddings IS NULL OR reserved_embeddings > 0",
            name="ck_content_identity_groups_reserved_embeddings_positive",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    identity_kind: Mapped[ContentIdentityKind] = mapped_column(
        SQLEnum(ContentIdentityKind, name="content_identity_kind"),
        nullable=False,
    )

    identity_algorithm: Mapped[ContentIdentityAlgorithm] = mapped_column(
        SQLEnum(ContentIdentityAlgorithm, name="content_identity_algorithm"),
        nullable=False,
    )

    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    pipeline_state: Mapped[ContentPipelineState] = mapped_column(
        SQLEnum(ContentPipelineState, name="content_pipeline_state"),
        nullable=False,
        default=ContentPipelineState.DISCOVERED,
        server_default=ContentPipelineState.DISCOVERED.name,
    )

    # Transient in-flight marker - see class docstring. Cleared on every
    # attempt's completion (success or failure), never a permanent record.
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Generation-fenced reservation primitives - see class docstring.
    # Schema/model only in this milestone; no reservation lifecycle
    # reads/writes reserved_embeddings yet.
    claim_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reserved_embeddings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Durable reservation ownership (Milestone 4 final correction) - see
    # class docstring. NULL exactly when reserved_embeddings is NULL
    # (CHECK-enforced). No ORM relationship() declared - this column is
    # read/written only via WorkerClaimService's raw UPDATE statements,
    # matching this model's existing convention for claimed_by/claimed_at.
    reserved_embeddings_batch_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ingestion_batches.id"), nullable=True
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

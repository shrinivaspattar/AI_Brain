from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Integer, String, Text, UniqueConstraint
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
    """

    __tablename__ = "content_identity_groups"
    __table_args__ = (
        UniqueConstraint(
            "identity_kind",
            "identity_algorithm",
            "identity_hash",
            name="uq_content_identity_groups_identity",
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

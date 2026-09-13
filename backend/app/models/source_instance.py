from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class CanonicalStatus(str, Enum):
    # The default and starting state for every SourceInstance in every
    # group, including one where every instance agrees byte-for-byte
    # and D2 raised no concerns at all. A valid, expected, and common
    # END state - not an error or a TODO.
    UNRESOLVED = "unresolved"
    # This specific SourceInstance has been explicitly marked as the
    # authoritative physical occurrence WITHIN ITS ContentIdentityGroup
    # - a relationship, never an intrinsic, global property of a file.
    # Requires canonical_status_reason/decided_by/decided_at (see the
    # CHECK constraint below) - never inferred from "no open review flag".
    CANONICAL = "canonical"
    # Requires its OWN explicit evidence/decision, exactly like
    # CANONICAL does. Never follows automatically from a sibling
    # instance being marked CANONICAL - a group may legitimately sit at
    # "one CANONICAL, N UNRESOLVED, zero NON_CANONICAL" forever.
    NON_CANONICAL = "non_canonical"


class SourceInstance(Base):
    """One physical observed occurrence of content - a specific file or
    archive member seen at a specific path, at classification time.

    THREE mutability classes on this one row (see AI_Brain_Architecture.md's
    Schema/Model Design section, round 5, for the full derivation):

    1. Fully immutable (set at creation, never touched again):
       classification_run_id, root_t7_path, member_path,
       evidence_snapshot, created_at. IMMUTABILITY ENFORCEMENT, stated
       precisely (review round 6, point 6): this is NOT a database
       CHECK constraint or trigger (Postgres CHECK constraints cannot
       compare a new value to an old one, and no trigger exists here).
       It is enforced by (a) documented contract - this docstring -
       and (b) the structural absence of any code path that updates
       `evidence_snapshot` after row creation: `SourceInstanceService.
       create_instance` is the ONLY place in this codebase that ever
       assigns it, and it does so exactly once, at construction. A
       direct `UPDATE` statement bypassing the ORM, or a future
       careless service method, could still mutate it - nothing at the
       database level would stop that. This matches this design's own
       precedent for `Document.content_hash`'s consistency invariant,
       which the frozen design explicitly states is "not enforced by a
       DB trigger in this design pass" - the same scope boundary,
       applied here rather than left merely implied.
    2. WRITE-ONCE (starts NULL, set exactly once, then fixed forever):
       content_identity_group_id. NOT archive-member-exclusive: D1's
       size-collision filter also skipped uniquely-sized LOOSE files,
       so those defer identity too, for the same reason an archive
       member does (see app.classification.content_identity_service).
    3. Freely mutable: canonical_status and its
       reason/decided_by/decided_at.

    `evidence_snapshot` is historical evidence captured from the
    discovery/classification inputs that existed at this row's
    classification_run's started_at - it is NEVER a live filesystem
    view, and no code may re-validate it against the T7's current
    state as if it were current truth. If the T7 changes later, this
    row does not know and must not claim to; a fresh observation is a
    NEW SourceInstance from a NEW ClassificationRun, never a mutation
    of this JSON.

    Symmetrically, `canonical_status_reason` is a DECISION ANNOTATION,
    not source evidence - it explains a human's (or a future policy's)
    choice about this row, and must never be read, copied into, or
    treated as if it were part of `evidence_snapshot`. The two live in
    genuinely different columns specifically so this distinction can
    never be blurred.

    WORKER CLAIM/LEASE FIELDS (added per "Controlled T7 -> AI_Brain
    Ingestion Design", `6491dad`, round 2, point 2 - schema-extension
    gate): `claimed_by`/`claimed_at` exist ONLY for the identity-
    resolution queue - a root-level instance (`content_identity_
    group_id IS NULL`, no parent link) whose content has never been
    read/hashed, e.g. a uniquely-sized loose T7 file D1 never hashed.
    Archive-member instances never use these fields: per the frozen
    design, the unit of claiming for an archive is its own parent
    `SourceInstance` (or, once one exists, its `ContentIdentityGroup`),
    not each member individually - one worker opens the archive once
    and creates/resolves every member's identity inside that single
    claimed unit of work. Same transient-marker semantics as
    `ContentIdentityGroup.claimed_by`/`claimed_at`: cleared on every
    attempt's completion, permanent history lives in `IngestionAttempt`.

    DISCOVERY-RUN-SCOPED MATERIALIZATION UNIQUENESS (added per "Scaled
    Real-T7 Ingestion - Implementation Design Pass", `2fab4b3`): a
    UNIQUE index on `(classification_run_id, root_t7_path,
    COALESCE(member_path, ''))` is independent defense-in-depth against
    ever materializing the same observation twice under one batch's
    `ClassificationRun` - the primary safeguard is a Postgres advisory
    lock held for the whole batch-creation transaction (see the frozen
    design's batch-creation-transaction section), and this constraint
    is the invariant of last resort if that lock is ever bypassed by a
    future bug. `COALESCE(..., '')` is required because `member_path`
    is NULL for every loose file - a plain `UNIQUE` constraint would
    not catch duplicate loose-file rows at all, since Postgres never
    considers two NULLs equal for uniqueness purposes. This does NOT
    prevent a later `DiscoveryRun`'s own, different `ClassificationRun`
    from legitimately re-observing the same `root_t7_path` (a different
    `classification_run_id` value is a different row in this
    constraint's key, by design).
    """

    __tablename__ = "source_instances"
    __table_args__ = (
        CheckConstraint(
            "canonical_status = 'UNRESOLVED' OR ("
            "canonical_status_reason IS NOT NULL "
            "AND canonical_status_decided_by IS NOT NULL "
            "AND canonical_status_decided_at IS NOT NULL"
            ")",
            name="ck_source_instances_canonical_status_requires_evidence",
        ),
        Index(
            "uq_source_instances_run_path_member",
            "classification_run_id",
            "root_t7_path",
            text("COALESCE(member_path, '')"),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    classification_run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("classification_runs.id"),
        nullable=False,
        index=True,
    )

    # NULLABLE, WRITE-ONCE - see class docstring. Null until this
    # occurrence's content identity is known.
    content_identity_group_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("content_identity_groups.id"),
        nullable=True,
        index=True,
    )

    # The outermost t7_file path (denormalized from the root
    # ProvenanceLink for query convenience, matching
    # DedupExecutionPlanAction's own denormalization convention).
    root_t7_path: Mapped[str] = mapped_column(Text, nullable=False)

    # Path within the innermost archive; null for a loose file
    # (root_t7_path already names it fully).
    member_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # IMMUTABLE. Copied verbatim at classification time: D1's
    # group_kind/group_key/category if this path was part of a D1
    # group, D2's inference_code/confidence/evidence_codes/
    # structured_facts/requires_human_review if D2 covered it, the
    # source-file hash if D1 computed one. Never rewritten after
    # creation - a correction is a new ClassificationRun, not an edit
    # here (matches DuplicateReview.evidence / Message.citations).
    evidence_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)

    canonical_status: Mapped[CanonicalStatus] = mapped_column(
        SQLEnum(CanonicalStatus, name="canonical_status"),
        nullable=False,
        default=CanonicalStatus.UNRESOLVED,
        server_default=CanonicalStatus.UNRESOLVED.name,
    )

    # REQUIRED (see CHECK constraint above) whenever canonical_status is
    # not UNRESOLVED - the explicit evidence/decision, never inferred.
    # A decision ANNOTATION, never source evidence (see class docstring).
    canonical_status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # "human" today; a future narrowly-scoped automated policy would
    # name itself here explicitly, never silently.
    canonical_status_decided_by: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    canonical_status_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Identity-resolution claim fields - see class docstring. Only ever
    # used for root-level, unhashed instances.
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

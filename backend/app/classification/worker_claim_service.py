from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session

from app.models.content_identity_group import ContentIdentityGroup, ContentPipelineState
from app.models.ingestion_attempt import IngestionAttempt, IngestionAttemptOutcome
from app.models.source_instance import SourceInstance

_ARCHIVE_SUFFIXES = (".zip", ".7z")


class WorkerClaimService:
    """Claims/releases work for the two identity-resolution and
    pipeline-advance queues described in "Controlled T7 -> AI_Brain
    Ingestion Design" (`6491dad`), rounds 1-2. Provides only the
    concurrency-safe claiming PRIMITIVE - no ingestion pipeline logic
    (extraction/normalization/chunking/embedding) is implemented here
    or anywhere in this schema-extension milestone; a future,
    separately-authorized gate builds the actual pipeline on top of
    these primitives.

    Claim mechanism: `SELECT ... FOR UPDATE SKIP LOCKED` (a row a
    second concurrent caller has already locked is skipped, never
    blocked on and never double-claimed) followed by an `UPDATE` in the
    SAME transaction - the row lock held by the SELECT is what makes
    this safe under real concurrency, not merely careful sequencing.
    This generalizes Chain 1's own `SELECT ... FOR UPDATE` execution-
    claiming primitive to a queue rather than a single-shot claim - it
    does not reuse Chain 1's state machine.

    A claim is considered stale - reclaimable by ANY worker, including
    a different one than originally claimed it - once `claimed_at` is
    older than `lease_duration`. There is no separate "recovery
    service": the same claim query that grants fresh work also
    reclaims stale work, by construction (the `WHERE claimed_by IS NULL
    OR claimed_at < :stale_before` clause below covers both cases in
    one query).

    `claimed_by`/`claimed_at` are cleared by `release_*` on every
    attempt's completion, success or failure - they are a transient,
    in-flight marker, never a permanent record. Permanent history
    belongs to `IngestionAttempt` (see `IngestionAttemptService`).
    """

    def __init__(self, db: Session):
        self.db = db

    def claim_content_identity_group(
        self,
        *,
        worker_id: str,
        eligible_pipeline_states: list[ContentPipelineState],
        lease_duration: timedelta,
        claiming_pipeline_state: ContentPipelineState | None = None,
    ) -> ContentIdentityGroup | None:
        """Claims one ContentIdentityGroup whose pipeline_state is in
        `eligible_pipeline_states` and whose claim (if any) is stale.

        `claiming_pipeline_state`, when given, is written atomically
        alongside the claim - used for the one step the frozen
        `ContentPipelineState` enum has an explicit in-progress marker
        for (`EXTRACTING`, claimed FROM `CLASSIFIED`). Every other step
        leaves `pipeline_state` at its current (completed-previous-
        step) value for the duration of the claim - `claimed_at` alone
        is the in-progress signal for those steps, since the frozen
        enum has no NORMALIZING/CHUNKING/EMBEDDING equivalent and this
        schema-extension gate does not add one.

        `claim_generation` is incremented by exactly 1 on every grant of
        this claim, fresh or reclaim - the fencing token a future
        embedding-reservation lifecycle uses to prove a delayed worker
        from an old generation can never act on a newer generation's
        state (see "Scaled Real-T7 Ingestion - Implementation Design
        Pass", `2fab4b3`, Rounds 2-3). The caller reads the returned
        row's `claim_generation` to learn the value it now holds.

        Returns None if no eligible, unclaimed-or-stale row exists.
        """
        stale_before = datetime.now(UTC) - lease_duration

        candidate_id = self.db.execute(
            select(ContentIdentityGroup.id)
            .where(
                ContentIdentityGroup.pipeline_state.in_(eligible_pipeline_states),
                (ContentIdentityGroup.claimed_by.is_(None))
                | (ContentIdentityGroup.claimed_at < stale_before),
            )
            .order_by(ContentIdentityGroup.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

        if candidate_id is None:
            return None

        values: dict = {
            "claimed_by": worker_id,
            "claimed_at": datetime.now(UTC),
            "claim_generation": ContentIdentityGroup.claim_generation + 1,
        }
        if claiming_pipeline_state is not None:
            values["pipeline_state"] = claiming_pipeline_state

        self.db.execute(
            update(ContentIdentityGroup)
            .where(ContentIdentityGroup.id == candidate_id)
            .values(**values)
        )
        self.db.commit()

        return self.db.get(ContentIdentityGroup, candidate_id)

    def release_content_identity_group_claim(
        self,
        group_id: int,
        *,
        new_pipeline_state: ContentPipelineState | None = None,
    ) -> None:
        """Clears a group's claim, optionally advancing pipeline_state
        in the same statement (the normal "attempt finished" path)."""
        values: dict = {"claimed_by": None, "claimed_at": None}
        if new_pipeline_state is not None:
            values["pipeline_state"] = new_pipeline_state

        self.db.execute(
            update(ContentIdentityGroup)
            .where(ContentIdentityGroup.id == group_id)
            .values(**values)
        )
        self.db.commit()

    def claim_source_instance_for_identity_resolution(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
    ) -> SourceInstance | None:
        """Claims one root-level, non-archive SourceInstance needing
        identity resolution: content_identity_group_id IS NULL.

        Excludes two categories of row that also have
        content_identity_group_id IS NULL but must never be resolved by
        this generic query, because `root_t7_path` would be the WRONG
        content to hash for either of them:

        - Archive-member instances (`member_path IS NOT NULL`): a
          member's own content lives inside the archive at
          `member_path`, not at `root_t7_path` (the archive's own
          path, shared identically across every one of its members).
          Per the frozen design, members are only ever identity-
          resolved together as part of claiming and processing the
          parent archive as one unit (see
          `claim_source_instance_for_archive_processing` /
          `ArchiveProcessingService`), never individually here.
        - Root-level archive instances themselves
          (`root_t7_path` ending in `_ARCHIVE_SUFFIXES`): an archive's
          own raw container bytes are never "content" to identity-
          resolve - only its extracted members are (see
          `claim_source_instance_for_archive_processing`'s docstring).
          Such a row legitimately and permanently keeps
          `content_identity_group_id IS NULL` even after being fully,
          successfully processed, so without this exclusion this query
          would eventually claim it once no other unresolved work
          remains and wrongly hash the archive's compressed bytes as
          if they were document content.
        """
        stale_before = datetime.now(UTC) - lease_duration

        not_archive_suffixed = ~or_(
            *(
                SourceInstance.root_t7_path.ilike(f"%{suffix}")
                for suffix in _ARCHIVE_SUFFIXES
            )
        )

        candidate_id = self.db.execute(
            select(SourceInstance.id)
            .where(
                SourceInstance.content_identity_group_id.is_(None),
                SourceInstance.member_path.is_(None),
                not_archive_suffixed,
                (SourceInstance.claimed_by.is_(None))
                | (SourceInstance.claimed_at < stale_before),
            )
            .order_by(SourceInstance.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

        if candidate_id is None:
            return None

        self.db.execute(
            update(SourceInstance)
            .where(SourceInstance.id == candidate_id)
            .values(claimed_by=worker_id, claimed_at=datetime.now(UTC))
        )
        self.db.commit()

        return self.db.get(SourceInstance, candidate_id)

    def release_source_instance_claim(self, instance_id: int) -> None:
        """Clears a SourceInstance's identity-resolution claim. Does
        NOT touch content_identity_group_id - that remains write-once,
        set only via ContentIdentityService.assign_content_identity."""
        self.db.execute(
            update(SourceInstance)
            .where(SourceInstance.id == instance_id)
            .values(claimed_by=None, claimed_at=None)
        )
        self.db.commit()

    def claim_source_instance_for_archive_processing(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
    ) -> SourceInstance | None:
        """Claims one TOP-LEVEL (T7-visible, `member_path IS NULL`)
        archive SourceInstance that has not yet been successfully
        processed. Never claims a nested archive discovered during
        extraction - per the frozen design, an archive's members
        (including nested archives) are all discovered and resolved
        together as part of processing the parent archive as one unit,
        never claimed independently.

        An archive's own SourceInstance never receives a
        content_identity_group_id (its raw container bytes are not
        "ingestible content" in the document sense - only its
        EXTRACTED members are) - so `content_identity_group_id IS NULL`
        alone cannot distinguish "not yet processed" from "successfully
        processed, correctly has no identity forever." The distinguishing
        signal reuses IngestionAttempt (no new schema column needed):
        an archive with at least one SUCCEEDED attempt recorded against
        it has already been fully processed and is excluded from this
        claim query - a legitimate reuse of the existing per-attempt
        audit table for exactly its intended purpose, not a new source
        of truth.
        """
        stale_before = datetime.now(UTC) - lease_duration

        already_processed = exists().where(
            IngestionAttempt.source_instance_id == SourceInstance.id,
            IngestionAttempt.outcome == IngestionAttemptOutcome.SUCCEEDED,
        )

        suffix_match = or_(
            *(
                SourceInstance.root_t7_path.ilike(f"%{suffix}")
                for suffix in _ARCHIVE_SUFFIXES
            )
        )

        candidate_id = self.db.execute(
            select(SourceInstance.id)
            .where(
                SourceInstance.member_path.is_(None),
                suffix_match,
                SourceInstance.content_identity_group_id.is_(None),
                ~already_processed,
                (SourceInstance.claimed_by.is_(None))
                | (SourceInstance.claimed_at < stale_before),
            )
            .order_by(SourceInstance.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

        if candidate_id is None:
            return None

        self.db.execute(
            update(SourceInstance)
            .where(SourceInstance.id == candidate_id)
            .values(claimed_by=worker_id, claimed_at=datetime.now(UTC))
        )
        self.db.commit()

        return self.db.get(SourceInstance, candidate_id)

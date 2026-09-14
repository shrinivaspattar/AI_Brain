from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session

from app.classification.resource_guard import BatchResourceGuard, ExpensiveOperationKind, GuardResult, GuardTier
from app.models.content_identity_group import ContentIdentityGroup, ContentPipelineState
from app.models.ingestion_attempt import IngestionAttempt, IngestionAttemptOutcome
from app.models.ingestion_batch import BatchStatus, IngestionBatch
from app.models.source_instance import SourceInstance

_ARCHIVE_SUFFIXES = (".zip", ".7z")


def _running_batch_exists_for_classification_run(classification_run_id: int):
    """A batch-admission gate, per Implementation Milestone 4's point
    12: 'RUNNING is the only batch state that admits new work,' enforced
    'before/within the claim operation rather than merely by caller
    convention.' Deliberately an EXISTS subquery re-evaluated fresh at
    each statement's own execution time (never a value read once and
    reused) - see `claim_source_instance_for_identity_resolution`'s
    docstring for exactly how this closes the claim-vs-pause/abort race
    rather than merely narrowing its window."""
    return exists().where(
        IngestionBatch.classification_run_id == classification_run_id,
        IngestionBatch.status == BatchStatus.RUNNING,
    )


class ReservationDenialReason(str, Enum):
    """Exactly the denial cases the frozen "### 8. Embedding reservation
    - fenced lifecycle" design distinguishes, plus the two batch-
    admission gates Milestone 4 adds (BATCH_NOT_RUNNING, resource-guard
    tiers) - never a generic catch-all string."""

    FENCED_OUT = "fenced_out"
    BATCH_NOT_FOUND = "batch_not_found"
    BATCH_NOT_RUNNING = "batch_not_running"
    ENVELOPE_EXHAUSTED = "envelope_exhausted"
    RESOURCE_GUARD_SOFT_STOP = "resource_guard_soft_stop"
    RESOURCE_GUARD_HARD_STOP = "resource_guard_hard_stop"


@dataclass(frozen=True)
class ReservationOutcome:
    """`reserved=False` always means "no reservation was placed and the
    claim was released" (see `WorkerClaimService.reserve_embeddings`) -
    never a partial reservation. `denial_reason` is populated exactly
    when `reserved` is `False`."""

    reserved: bool
    amount: int | None = None
    denial_reason: ReservationDenialReason | None = None
    guard_result: GuardResult | None = None


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

    BATCH-AWARE CLAIMING (added per Implementation Milestone 4,
    "Batch-Aware Worker Claim + Generation-Fenced Reservation
    Integration" - see "Scaled Real-T7 Ingestion - Implementation
    Design Pass" `2fab4b3`, "### 10. Claim / batch interaction"):
    `claim_source_instance_for_identity_resolution` and `claim_source_
    instance_for_archive_processing` gain an OPTIONAL `classification_
    run_id` parameter. `None` (the default) preserves this method's
    exact pre-Milestone-4 behavior byte-for-byte - the existing, already
    -committed non-batch pipeline (`identity_resolution_service.py`,
    `archive_processing_service.py`) calls these with no argument and
    is completely unaffected. When given, the claim predicate gains
    `SourceInstance.classification_run_id = :classification_run_id` as
    one more `AND` term (exactly the frozen doc's own wording) PLUS an
    admission gate requiring that classification_run's `IngestionBatch`
    to currently be `RUNNING` (Milestone 4 point 12) - both re-checked
    inside the final `UPDATE`'s own `WHERE` clause, not merely the
    earlier candidate `SELECT`, so a batch that pauses/aborts between
    the two statements is still honored (see that method's docstring).

    `claim_content_identity_group` deliberately receives NO batch
    parameter and is NOT changed by this milestone - the frozen
    "### 8. Embedding reservation" design explicitly proves this claim
    is correctly GLOBAL regardless of batch ("the claim is globally
    exclusive... so batch-level ambiguity cannot arise structurally"),
    since one `ContentIdentityGroup` can legitimately be referenced by
    `SourceInstance` rows from many different batches at once. Batch
    admission for THAT queue instead lives at the embedding-reservation
    step (`reserve_embeddings`, which does take a `batch_id`) - the
    correct, frozen-design-verified layer for it, not the group claim
    itself. See `reserve_embeddings`'s docstring for the full generation
    -fenced reservation lifecycle this milestone adds.

    SourceInstance CLAIM-GENERATION FENCING (added by Implementation
    Milestone 5, generalizing `ContentIdentityGroup.claim_generation`
    to `SourceInstance`): both `claim_source_instance_for_identity_
    resolution` and `claim_source_instance_for_archive_processing`
    increment `SourceInstance.claim_generation` by exactly 1 on every
    grant (fresh or stale-reclaim), in the SAME atomic claim `UPDATE`
    - the column lives on the model, so both claim queues are fenced
    uniformly, not just the archive-processing one. `release_source_
    instance_claim` requires and fences on this value, exactly
    mirroring `release_content_identity_group_claim`'s Milestone-4
    hardening. This closes a real ABA hole: a worker delayed (not
    merely crashed) past its lease could otherwise call release AFTER
    stale-claim recovery has already reclaimed the row for a different,
    actively-working worker, wrongly clearing that worker's live claim.
    See "Scaled Real-T7 Ingestion - Milestone 5 Design" (Design
    Correction Pass, section 2) for the full derivation.
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
        this claim, fresh or reclaim - the fencing token the embedding-
        reservation lifecycle (`reserve_embeddings` et al., added by
        Milestone 4) uses to prove a delayed worker from an old
        generation can never act on a newer generation's state. The
        caller reads the returned row's `claim_generation` to learn the
        value it now holds.

        ABANDONED-RESERVATION RECOVERY (Milestone 4's final correction,
        "Durable Reservation Ownership"): if the row being reclaimed
        carries a live reservation (`reserved_embeddings IS NOT NULL`)
        left behind by whatever generation held it before, THIS SAME
        transaction reads its owning `reserved_embeddings_batch_id`
        fresh under the row lock just taken above, credits that exact
        amount back to that exact `IngestionBatch.embeddings_reserved`,
        and clears both reservation columns - atomically, in the same
        transaction that advances the generation and reassigns
        `claimed_by`/`claimed_at`. This is the ONLY place an abandoned
        reservation is ever recovered from - never inferred from a
        remembered worker, a remembered generation, or caller-supplied
        history, always the row's own freshly-locked, currently-
        persisted state. A fresh (never-claimed) row or one with no
        outstanding reservation simply has nothing to recover - this
        step is then a no-op, unchanged from the pre-correction
        behavior. Crediting back is unconditional on the owning batch's
        `status` (even an already-`ABORTED` batch's counter is still
        correctly reconciled - this is bookkeeping correction, never a
        new-work admission decision).

        Returns None if no eligible, unclaimed-or-stale row exists.
        """
        stale_before = datetime.now(UTC) - lease_duration

        candidate = self.db.execute(
            select(
                ContentIdentityGroup.id,
                ContentIdentityGroup.reserved_embeddings,
                ContentIdentityGroup.reserved_embeddings_batch_id,
            )
            .where(
                ContentIdentityGroup.pipeline_state.in_(eligible_pipeline_states),
                (ContentIdentityGroup.claimed_by.is_(None))
                | (ContentIdentityGroup.claimed_at < stale_before),
            )
            .order_by(ContentIdentityGroup.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).one_or_none()

        if candidate is None:
            return None

        candidate_id, abandoned_amount, abandoned_batch_id = candidate

        if abandoned_amount is not None:
            self.db.execute(
                update(IngestionBatch)
                .where(IngestionBatch.id == abandoned_batch_id)
                .values(embeddings_reserved=IngestionBatch.embeddings_reserved - abandoned_amount)
            )

        values: dict = {
            "claimed_by": worker_id,
            "claimed_at": datetime.now(UTC),
            "claim_generation": ContentIdentityGroup.claim_generation + 1,
            "reserved_embeddings": None,
            "reserved_embeddings_batch_id": None,
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
        claim_generation: int,
        new_pipeline_state: ContentPipelineState | None = None,
    ) -> bool:
        """Clears a group's claim, optionally advancing pipeline_state
        in the same statement (the normal "attempt finished" path).

        `claim_generation` is REQUIRED (added per Implementation
        Milestone 4) and fences this release to `WHERE id = :group_id
        AND claim_generation = :claim_generation` - closing a real ABA
        hole in the pre-Milestone-4 version of this method, which
        cleared `claimed_by`/`claimed_at` unconditionally by `group_id`
        alone. Without this fence, a worker delayed (not merely
        crashed) past its lease could call this AFTER stale-claim
        recovery has already reclaimed the row into a new generation
        for a different, actively-working worker - the delayed caller's
        release would then wrongly clear the NEW worker's live claim.
        Every existing caller already holds the group's current
        generation from its own `claim_content_identity_group` call, so
        this is a purely additive safety check for the normal
        (non-adversarial) case - the value passed is always the exact
        generation the row already has, so the fenced `WHERE` matches
        exactly as before.

        Returns `True` if this call's generation still matched (a real
        release happened) or `False` if the row had already moved to a
        later generation (a stale, safe no-op - never a raised
        exception). Callers that do not need to distinguish the two may
        ignore the return value, matching this method's pre-Milestone-4
        `None`-returning callers unchanged."""
        values: dict = {"claimed_by": None, "claimed_at": None}
        if new_pipeline_state is not None:
            values["pipeline_state"] = new_pipeline_state

        applied = (
            self.db.execute(
                update(ContentIdentityGroup)
                .where(
                    ContentIdentityGroup.id == group_id,
                    ContentIdentityGroup.claim_generation == claim_generation,
                )
                .values(**values)
                .returning(ContentIdentityGroup.id)
            ).first()
            is not None
        )
        self.db.commit()
        return applied

    def claim_source_instance_for_identity_resolution(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
        classification_run_id: int | None = None,
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

        `classification_run_id` (Milestone 4 addition, default `None` =
        pre-Milestone-4 behavior, unchanged): when given, restricts
        candidates to that run AND requires its `IngestionBatch` to be
        currently `RUNNING`. Both conditions are re-checked in the
        final `UPDATE`'s own `WHERE` clause (not just the candidate
        `SELECT` above) so a batch that pauses/aborts in the gap between
        the two statements is still honored - Postgres re-evaluates a
        statement's own `WHERE` against whatever is committed at THAT
        statement's execution time, so the `UPDATE` sees the batch's
        true, current state even if it changed after the `SELECT` ran.
        If the `UPDATE` fails to match for this reason, the transaction
        is rolled back and `None` is returned - a clean "no work
        available right now," never a claim on a non-RUNNING batch.
        """
        stale_before = datetime.now(UTC) - lease_duration

        not_archive_suffixed = ~or_(
            *(
                SourceInstance.root_t7_path.ilike(f"%{suffix}")
                for suffix in _ARCHIVE_SUFFIXES
            )
        )

        select_conditions = [
            SourceInstance.content_identity_group_id.is_(None),
            SourceInstance.member_path.is_(None),
            not_archive_suffixed,
            (SourceInstance.claimed_by.is_(None))
            | (SourceInstance.claimed_at < stale_before),
        ]
        update_conditions = [SourceInstance.claimed_by.is_(None) | (SourceInstance.claimed_at < stale_before)]
        if classification_run_id is not None:
            select_conditions.append(SourceInstance.classification_run_id == classification_run_id)
            select_conditions.append(_running_batch_exists_for_classification_run(classification_run_id))
            update_conditions.append(_running_batch_exists_for_classification_run(classification_run_id))

        candidate_id = self.db.execute(
            select(SourceInstance.id)
            .where(*select_conditions)
            .order_by(SourceInstance.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

        if candidate_id is None:
            return None

        applied = (
            self.db.execute(
                update(SourceInstance)
                .where(SourceInstance.id == candidate_id, *update_conditions)
                .values(
                    claimed_by=worker_id,
                    claimed_at=datetime.now(UTC),
                    claim_generation=SourceInstance.claim_generation + 1,
                )
                .returning(SourceInstance.id)
            ).first()
            is not None
        )
        if not applied:
            self.db.rollback()
            return None

        self.db.commit()
        return self.db.get(SourceInstance, candidate_id)

    def release_source_instance_claim(self, instance_id: int, *, claim_generation: int) -> bool:
        """Clears a SourceInstance's identity-resolution/archive-
        processing claim, fenced to `claim_generation` (Milestone 5) -
        exactly mirroring `release_content_identity_group_claim`'s
        Milestone-4 hardening. Does NOT touch content_identity_group_id
        - that remains write-once, set only via ContentIdentityService.
        assign_content_identity.

        Returns `True` if this call's generation still matched (a real
        release happened) or `False` if the row had already moved to a
        later generation (a stale, safe no-op - never a raised
        exception, never a wrongful clear of a newer owner's claim)."""
        applied = (
            self.db.execute(
                update(SourceInstance)
                .where(
                    SourceInstance.id == instance_id,
                    SourceInstance.claim_generation == claim_generation,
                )
                .values(claimed_by=None, claimed_at=None)
                .returning(SourceInstance.id)
            ).first()
            is not None
        )
        self.db.commit()
        return applied

    def claim_source_instance_for_archive_processing(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
        classification_run_id: int | None = None,
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

        `classification_run_id` (Milestone 4 addition, default `None` =
        pre-Milestone-4 behavior, unchanged) - see `claim_source_
        instance_for_identity_resolution`'s docstring for the exact
        batch-scoping/admission semantics; identical here.
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

        select_conditions = [
            SourceInstance.member_path.is_(None),
            suffix_match,
            SourceInstance.content_identity_group_id.is_(None),
            ~already_processed,
            (SourceInstance.claimed_by.is_(None))
            | (SourceInstance.claimed_at < stale_before),
        ]
        update_conditions = [SourceInstance.claimed_by.is_(None) | (SourceInstance.claimed_at < stale_before)]
        if classification_run_id is not None:
            select_conditions.append(SourceInstance.classification_run_id == classification_run_id)
            select_conditions.append(_running_batch_exists_for_classification_run(classification_run_id))
            update_conditions.append(_running_batch_exists_for_classification_run(classification_run_id))

        candidate_id = self.db.execute(
            select(SourceInstance.id)
            .where(*select_conditions)
            .order_by(SourceInstance.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

        if candidate_id is None:
            return None

        applied = (
            self.db.execute(
                update(SourceInstance)
                .where(SourceInstance.id == candidate_id, *update_conditions)
                .values(
                    claimed_by=worker_id,
                    claimed_at=datetime.now(UTC),
                    claim_generation=SourceInstance.claim_generation + 1,
                )
                .returning(SourceInstance.id)
            ).first()
            is not None
        )
        if not applied:
            self.db.rollback()
            return None

        self.db.commit()
        return self.db.get(SourceInstance, candidate_id)

    # -- Embedding reservation - fenced lifecycle (Implementation ----
    # -- Milestone 4, hardened by its final correction "Durable
    # -- Reservation Ownership"). Exact mechanism per "Scaled Real-T7
    # -- Ingestion - Implementation Design Pass" (`2fab4b3`), "### 8.
    # -- Embedding reservation - fenced lifecycle, including crash
    # -- recovery." `reserved_embeddings`/`claim_generation` already
    # -- existed on ContentIdentityGroup (Milestone 1);
    # -- `reserved_embeddings_batch_id` is this correction's one new,
    # -- additive column (migration `b33606f7d5e8`).
    #
    # GAP CLOSED (was previously flagged as an honest limitation, now
    # resolved): earlier drafts of this milestone had no durable record
    # of WHICH batch a reservation belonged to, so an ordinary stale-
    # claim reclaim (inside `claim_content_identity_group`) could not
    # credit an abandoned reservation back to its true owning batch.
    # `reserved_embeddings_batch_id` closes this exactly: every
    # reservation now durably names its own owning `IngestionBatch`,
    # and `claim_content_identity_group`'s reclaim path reads that
    # ownership fresh under its own row lock and reconciles the correct
    # batch's counter atomically, in the same transaction that advances
    # the generation - see that method's docstring for the exact
    # sequence. `release_embedding_reservation` below likewise reads
    # ownership from the row itself rather than accepting a caller-
    # supplied `batch_id`, per the correction's explicit instruction to
    # never infer it from a remembered worker/generation/history.

    def reserve_embeddings(
        self,
        *,
        group_id: int,
        my_generation: int,
        batch_id: int,
        n: int,
        guard: BatchResourceGuard | None = None,
    ) -> ReservationOutcome:
        """`reserve (generation N)` from the frozen lifecycle: an
        UNCONDITIONAL `SET reserved_embeddings = :n` on the group,
        fenced to `claim_generation = :my_generation`, plus an atomic
        conditional increment of `IngestionBatch.embeddings_reserved`
        bounded by `max_embeddings` - never read-then-update (point 10
        of the Milestone 4 authorization). Both writes happen in the
        SAME transaction; if either fails to match, the whole
        transaction is rolled back (undoing any partial write) and the
        claim is released via the fenced `release_content_identity_
        group_claim` - exactly "roll back whichever part succeeded;
        release claim" from the frozen pseudocode. Never partially
        reserves (point 5a): `reserved` is either `True` with the full
        `n`, or `False` with nothing written at all.

        `guard`, if given, is consulted FIRST via `check_before_
        expensive_operation(batch, EMBEDDING)` (Milestone 4 point 13) -
        a non-`NORMAL` tier denies the reservation and releases the
        claim before any write is attempted. This method does not
        mutate batch `status` itself (that remains `BatchControlService`
        's responsibility) - it only refuses admission, matching "use
        the already-committed BatchResourceGuard only as the admission
        control signal... do not duplicate the resource-guard
        implementation."

        The batch-side `UPDATE` also requires `status = RUNNING`
        (Milestone 4 point 12), re-checked at this exact moment even if
        `guard` was not supplied.
        """
        batch = self.db.get(IngestionBatch, batch_id)
        if batch is None:
            self.release_content_identity_group_claim(group_id, claim_generation=my_generation)
            return ReservationOutcome(reserved=False, denial_reason=ReservationDenialReason.BATCH_NOT_FOUND)

        if guard is not None:
            guard_result = guard.check_before_expensive_operation(batch, ExpensiveOperationKind.EMBEDDING)
            if guard_result.tier is not GuardTier.NORMAL:
                self.release_content_identity_group_claim(group_id, claim_generation=my_generation)
                denial = (
                    ReservationDenialReason.RESOURCE_GUARD_HARD_STOP
                    if guard_result.tier is GuardTier.HARD_STOP
                    else ReservationDenialReason.RESOURCE_GUARD_SOFT_STOP
                )
                return ReservationOutcome(reserved=False, denial_reason=denial, guard_result=guard_result)

        group_applied = (
            self.db.execute(
                update(ContentIdentityGroup)
                .where(ContentIdentityGroup.id == group_id, ContentIdentityGroup.claim_generation == my_generation)
                .values(reserved_embeddings=n, reserved_embeddings_batch_id=batch_id)
                .returning(ContentIdentityGroup.id)
            ).first()
            is not None
        )
        if not group_applied:
            self.db.rollback()
            self.release_content_identity_group_claim(group_id, claim_generation=my_generation)
            return ReservationOutcome(reserved=False, denial_reason=ReservationDenialReason.FENCED_OUT)

        batch_result = self.db.execute(
            update(IngestionBatch)
            .where(
                IngestionBatch.id == batch_id,
                IngestionBatch.status == BatchStatus.RUNNING,
                IngestionBatch.embeddings_reserved + n <= IngestionBatch.max_embeddings,
            )
            .values(embeddings_reserved=IngestionBatch.embeddings_reserved + n)
            .returning(IngestionBatch.embeddings_reserved)
        ).first()

        if batch_result is None:
            self.db.rollback()
            self.release_content_identity_group_claim(group_id, claim_generation=my_generation)
            denial = (
                ReservationDenialReason.BATCH_NOT_RUNNING
                if batch.status is not BatchStatus.RUNNING
                else ReservationDenialReason.ENVELOPE_EXHAUSTED
            )
            return ReservationOutcome(reserved=False, denial_reason=denial)

        self.db.commit()
        return ReservationOutcome(reserved=True, amount=n)

    def consume_embedding_reservation(
        self,
        *,
        group_id: int,
        my_generation: int,
        new_pipeline_state: ContentPipelineState,
    ) -> bool:
        """`consume (generation N, success)` from the frozen lifecycle:
        clears the reservation (fenced to `my_generation`), then
        releases the claim - ALSO fenced to the exact same generation,
        advancing `pipeline_state` in that same statement.
        `IngestionBatch.embeddings_reserved` is deliberately NOT
        decremented here - "monotonic (unchanged)" per the frozen
        design; a consumed reservation was real, successful work, not
        capacity to give back. Returns `False` (a safe no-op, never an
        exception) if `my_generation` no longer matches - this caller is
        stale and must not proceed to persist chunk embeddings as if it
        still owned the row (a real caller is expected to check this
        BEFORE calling `EmbeddingClient.embed()` at all, per the frozen
        design's own sequencing)."""
        reservation_cleared = (
            self.db.execute(
                update(ContentIdentityGroup)
                .where(
                    ContentIdentityGroup.id == group_id,
                    ContentIdentityGroup.claim_generation == my_generation,
                    ContentIdentityGroup.reserved_embeddings.is_not(None),
                )
                .values(reserved_embeddings=None, reserved_embeddings_batch_id=None)
                .returning(ContentIdentityGroup.id)
            ).first()
            is not None
        )
        if not reservation_cleared:
            self.db.rollback()
            return False

        self.db.commit()
        return self.release_content_identity_group_claim(
            group_id, claim_generation=my_generation, new_pipeline_state=new_pipeline_state
        )

    def release_embedding_reservation(
        self,
        *,
        group_id: int,
        my_generation: int,
    ) -> bool:
        """The SAME idempotent, ownership-checked operation the frozen
        design uses for BOTH the failure path (`embed()` raised) and
        stale-reservation recovery ("BOTH paths use the exact SAME
        idempotent, OWNERSHIP-CHECKED operation") - this one method
        serves both callers; there is no separate "recovery service"
        for reservations, matching Milestone 4 point 9's explicit
        instruction not to invent one.

        DURABLE OWNERSHIP (Milestone 4's final correction): `batch_id`
        is NO LONGER caller-supplied - it is read from the row's own
        `reserved_embeddings_batch_id`, fresh, under the `SELECT ...
        FOR UPDATE` lock taken below, exactly matching the correction's
        instruction to "never infer the owning batch from a remembered
        worker value, a remembered generation, or caller-provided
        historical state." This makes the method correct and safe for
        BOTH callers identically: the same worker releasing its own
        just-failed reservation, or an entirely different, later caller
        (e.g. after `claim_content_identity_group`'s own built-in
        abandoned-reservation recovery - see that method's docstring -
        already handled the common stale-reclaim case automatically;
        this method remains available for any caller that still needs
        to release a reservation it holds under a generation it can
        name).

        Fenced by `id = :group_id AND claim_generation = :my_generation
        AND reserved_embeddings IS NOT NULL`. If a row is returned,
        credits the released amount back to the batch this row itself
        names, clears both reservation columns, and releases the claim
        (fenced, same generation). If no row is returned - EITHER
        already NULL (released by a concurrent/prior call for the SAME
        generation) OR the generation has already moved on (this caller
        is stale) - both cases are indistinguishable to the caller and
        both correctly require no further action: returns `False`,
        touches no batch counter, raises nothing.

        IMPLEMENTATION NOTE: `UPDATE ... SET reserved_embeddings = NULL
        ... RETURNING reserved_embeddings` would return the NEW (NULL)
        value, not the pre-update amount - Postgres `RETURNING` always
        reflects the row AFTER the write. The pre-update amount (and
        owning batch id) are therefore read via `SELECT ... FOR UPDATE`
        first (taking the row lock, fencing on the identical `id AND
        claim_generation` predicate), THEN cleared - both statements
        inside the same transaction, so the lock held by the `SELECT`
        makes the two-step sequence exactly as safe under real
        concurrency as a single fenced `UPDATE` would be."""
        current = self.db.execute(
            select(ContentIdentityGroup.reserved_embeddings, ContentIdentityGroup.reserved_embeddings_batch_id)
            .where(
                ContentIdentityGroup.id == group_id,
                ContentIdentityGroup.claim_generation == my_generation,
            )
            .with_for_update()
        ).one_or_none()

        if current is None or current[0] is None:
            self.db.rollback()
            return False

        current_reservation, owning_batch_id = current

        self.db.execute(
            update(ContentIdentityGroup)
            .where(
                ContentIdentityGroup.id == group_id,
                ContentIdentityGroup.claim_generation == my_generation,
            )
            .values(reserved_embeddings=None, reserved_embeddings_batch_id=None)
        )
        self.db.execute(
            update(IngestionBatch)
            .where(IngestionBatch.id == owning_batch_id)
            .values(embeddings_reserved=IngestionBatch.embeddings_reserved - current_reservation)
        )
        self.db.commit()
        self.release_content_identity_group_claim(group_id, claim_generation=my_generation)
        return True

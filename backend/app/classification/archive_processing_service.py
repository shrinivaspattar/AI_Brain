from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classification.content_identity_service import ContentIdentityService
from app.classification.eligibility_service import IngestionEligibility, classify_eligibility
from app.classification.ingestion_attempt_service import IngestionAttemptService
from app.classification.policy_evaluator import (
    classify_risk_tier_estimated,
    classify_source_category,
    classify_workload_category,
)
from app.classification.resource_guard import BatchResourceGuard, ExpensiveOperationKind, GuardTier
from app.classification.source_instance_service import ProvenanceStep, SourceInstanceService
from app.classification.worker_claim_service import WorkerClaimService
from app.classification.workspace import (
    initial_pipeline_state_for_eligibility,
    write_workspace_content,
)
from app.ingestion.archive import ArchiveExtractor
from app.ingestion.scanner import DiscoveredFile
from app.models.content_identity_group import ContentIdentityAlgorithm, ContentIdentityKind
from app.models.ingestion_attempt import IngestionAttemptOutcome, IngestionFailureCode
from app.models.ingestion_batch import BatchStatus, IngestionBatch
from app.models.provenance_link import ProvenanceLinkKind
from app.models.source_instance import SourceInstance

_ARCHIVE_SUFFIXES = (".zip", ".7z")


def _staging_root(workspace_root: Path, root_source_instance_id: int) -> Path:
    """The frozen staging contract (Milestone 5 design, section 3):
    isolated per ROOT SourceInstance, always under workspace_root -
    never a T7 path. Stable across retries/resumes since it is keyed by
    a durable DB id, never a worker id, timestamp, or attempt counter."""
    return workspace_root / "_staging" / f"archive_{root_source_instance_id}"


def _nested_staging_dir(workspace_root: Path, root_source_instance_id: int, full_member_path: str) -> str:
    """Deterministic, collision-safe nested-archive staging path: a
    stable hash of the member's own full internal path, never the raw
    (attacker/data-controlled) member name itself used as a filesystem
    path component. Returns a directory NAME (relative to the staging
    root), not a resolved Path, matching the caller's join pattern."""
    digest = hashlib.sha256(full_member_path.encode("utf-8")).hexdigest()[:16]
    return str(_staging_root(workspace_root, root_source_instance_id) / digest)


def _is_max_depth_exceeded_message(message: str) -> bool:
    return "nesting exceeds max_depth" in message


class _ByteAccumulator:
    """A mutable running total threaded through the recursive walk -
    the whole-claim-attempt `actual_durable_extracted_bytes` total
    (Milestone 5 design, section 6), summed across every recursion
    level actually reached, reconciled to `IngestionBatch.extracted_
    bytes_consumed` exactly once, at the end of the whole attempt."""

    def __init__(self) -> None:
        self.total_bytes = 0

    def add(self, n: int) -> None:
        self.total_bytes += n


@dataclass(frozen=True)
class _PreflightResult:
    admitted: bool
    estimated_bytes: int = 0


class ArchiveProcessingService:
    """Recursively processes ONE claimed, top-level archive
    SourceInstance: opens it, enumerates members (reusing the existing,
    already-tested `ArchiveExtractor` and its safety guards - path-
    traversal rejection, expansion-ratio bomb guard, disk-space
    reserve, symlink/junction/socket-member rejection, and the
    Milestone 5 post-extraction file-type audit - unmodified in their
    own logic), creates a SourceInstance for every member (including
    nested archives, which are themselves recursed into up to
    `max_depth`), and resolves the content identity of every LEAF
    (non-archive) member immediately - per the frozen design, a
    member's identity is only ever knowable AFTER it has been read,
    which extraction just did.

    CRASH SAFETY, stated precisely (this is what makes "archive crash
    halfway -> resumable without duplicate members" true): this service
    does NOT wrap the whole recursive walk in one database transaction
    (the existing sub-services it calls - SourceInstanceService,
    ContentIdentityService - each commit their own unit of work, per
    their documented transaction contracts, and are not composed inside
    a shared uncommitted transaction here). Instead, safety comes from
    IDEMPOTENCY: before creating a SourceInstance for a given member,
    this service checks whether one already exists for the exact same
    (classification_run_id, root_t7_path, member_path) triple - a
    member already created (and possibly already identity-resolved) by
    an earlier, crashed attempt is found and either skipped entirely
    (if its identity was already resolved) or completed (if the crash
    landed between member-creation and identity-resolution). Extraction
    itself (writing files into the workspace) is deterministic and
    safe to repeat - a resumed attempt may re-extract an archive it
    already extracted before, but never creates a duplicate
    SourceInstance or ContentIdentityGroup as a result.

    The outermost archive's OWN SourceInstance never receives a
    content_identity_group_id (see WorkerClaimService.claim_source_
    instance_for_archive_processing's docstring for why "already
    processed" is tracked via IngestionAttempt instead).

    BATCH INTEGRATION (Implementation Milestone 5, "Scaled Real-T7
    Ingestion - Milestone 5 Design: Archive Processing / Extraction",
    Design Correction Pass): `classification_run_id`/`guard` are
    OPTIONAL, default `None` - preserving this service's exact
    pre-Milestone-5 behavior byte-for-byte for any caller that omits
    them (mirroring the identical optionality pattern Milestone 4
    established for `WorkerClaimService`'s own SourceInstance claim
    methods). When `classification_run_id` is given, this service:
    - claims batch-scoped, RUNNING-only (via the existing Milestone 4
      claim predicate, unchanged);
    - performs pre-flight extracted-bytes envelope admission (an
      atomic conditional UPDATE, never read-then-update) before ANY
      extraction is attempted;
    - reconciles the batch's durable `extracted_bytes_consumed`
      counter to the REAL measured total once the whole recursive walk
      completes, or releases the reservation in full if extraction
      fails;
    - consults `guard` (a `BatchResourceGuard`), if given, as a pure
      admission-control signal before admitting - never duplicating the
      guard's own disk-check implementation here.
    Batch-envelope admission is decided ONCE, before the recursive walk
    begins - not re-checked per nested level (the frozen design's own
    "pre-flight algorithm" names this as a single, upfront gate; a
    future orchestration milestone, not yet built, is responsible for
    re-checking batch state before invoking this service again for the
    NEXT archive claim).

    STAGING (Milestone 5): extraction now always targets an isolated
    `_staging/archive_<id>/` tree (never a workspace-root-adjacent
    directory named after the archive's own stem, which could collide
    across sibling archives) - unconditionally, for every caller,
    batch-aware or not, since this closes a real staging-hygiene gap
    rather than introducing new batch-specific behavior. Nested
    archives extract into a deterministic, collision-safe subdirectory
    named by a stable hash of their own full internal member path,
    never the raw archive-member name itself. The staging tree is
    deleted unconditionally at the START of every claim attempt
    (idempotent crash-orphan cleanup) and again at the END (success or
    failure alike) - it is always transient, never the location
    Document/DocumentChunk content is read from (that remains the
    existing, unchanged `workspace_content_path` convention).
    """

    def __init__(self, db: Session):
        self.db = db
        self.claims = WorkerClaimService(db)
        self.identity = ContentIdentityService(db)
        self.attempts = IngestionAttemptService(db)
        self.instances = SourceInstanceService(db)
        self.extractor = ArchiveExtractor()

    def process_next_archive(
        self,
        *,
        worker_id: str,
        workspace_root: Path,
        lease_duration: timedelta = timedelta(minutes=10),
        max_depth: int = 10,
        classification_run_id: int | None = None,
        guard: BatchResourceGuard | None = None,
    ) -> SourceInstance | None:
        instance = self.claims.claim_source_instance_for_archive_processing(
            worker_id=worker_id,
            lease_duration=lease_duration,
            classification_run_id=classification_run_id,
        )
        if instance is None:
            return None

        my_generation = instance.claim_generation
        staging_root = _staging_root(workspace_root, instance.id)
        shutil.rmtree(staging_root, ignore_errors=True)  # idempotent crash-orphan cleanup

        batch: IngestionBatch | None = None
        if classification_run_id is not None:
            batch = self.db.execute(
                select(IngestionBatch).where(IngestionBatch.classification_run_id == classification_run_id)
            ).scalar_one_or_none()

        try:
            estimated_bytes = 0
            if batch is not None:
                preflight = self._preflight_admit(batch=batch, instance=instance, guard=guard)
                if not preflight.admitted:
                    # Deferred, not failed - matches the frozen numeric
                    # pass's EXTRACTED_BYTES_ENVELOPE_EXHAUSTED /
                    # resource-guard disposition exactly: no
                    # IngestionAttempt is recorded, nothing was decided
                    # about this item.
                    return None
                estimated_bytes = preflight.estimated_bytes

            self._process_claimed_archive(
                instance,
                worker_id=worker_id,
                workspace_root=workspace_root,
                max_depth=max_depth,
                batch=batch,
                estimated_bytes=estimated_bytes,
            )
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
            self.claims.release_source_instance_claim(instance.id, claim_generation=my_generation)

        self.db.refresh(instance)
        return instance

    def _preflight_admit(
        self,
        *,
        batch: IngestionBatch,
        instance: SourceInstance,
        guard: BatchResourceGuard | None,
    ) -> _PreflightResult:
        """Pre-flight envelope calculation (Milestone 5 design, section
        4), exactly once per root claim attempt. `guard` is consulted
        first (pure admission signal, never duplicating its own disk
        check); batch RUNNING is re-checked atomically even when
        `max_extracted_bytes IS NULL` (a batch class admitting no
        archives, or one that opted out of the byte envelope, must
        still refuse admission once no longer RUNNING)."""
        if guard is not None:
            guard_result = guard.check_before_expensive_operation(batch, ExpensiveOperationKind.ARCHIVE_EXTRACTION)
            if guard_result.tier is not GuardTier.NORMAL:
                return _PreflightResult(admitted=False)

        if batch.max_extracted_bytes is None:
            # No byte envelope applies - a real, meaningful "not
            # applicable," never treated as "unlimited" by accident
            # (matches IngestionBatch's own frozen column semantics).
            # No counter to protect here, so a plain read-check is
            # sufficient (not a TOCTOU concern this codebase otherwise
            # guards against with conditional UPDATEs) - if the batch
            # pauses a moment after this check passes, that is the
            # already-accepted "an already-granted extraction cannot be
            # retroactively interrupted" case (design section 16), not
            # a race this check is responsible for closing.
            still_running = (
                self.db.execute(
                    select(IngestionBatch.id).where(
                        IngestionBatch.id == batch.id, IngestionBatch.status == BatchStatus.RUNNING
                    )
                ).first()
                is not None
            )
            if not still_running:
                return _PreflightResult(admitted=False)
            return _PreflightResult(admitted=True, estimated_bytes=0)

        estimated_bytes = instance.evidence_snapshot.get("d0_declared_size_bytes") or 0
        applied_row = self.db.execute(
            IngestionBatch.__table__.update()
            .where(
                IngestionBatch.id == batch.id,
                IngestionBatch.status == BatchStatus.RUNNING,
                IngestionBatch.extracted_bytes_consumed + estimated_bytes <= IngestionBatch.max_extracted_bytes,
            )
            .values(extracted_bytes_consumed=IngestionBatch.extracted_bytes_consumed + estimated_bytes)
            .returning(IngestionBatch.id)
        ).first()
        if applied_row is None:
            self.db.rollback()
            return _PreflightResult(admitted=False)
        self.db.commit()
        return _PreflightResult(admitted=True, estimated_bytes=estimated_bytes)

    def _release_extracted_bytes_reservation(self, *, batch_id: int, amount: int) -> None:
        """Extraction failure path (Milestone 5 design, section 6):
        release the ORIGINAL declared/estimated reservation exactly -
        symmetric to the frozen "no actual bytes durably exist to
        reconcile against" rule."""
        if amount == 0:
            return
        self.db.execute(
            IngestionBatch.__table__.update()
            .where(IngestionBatch.id == batch_id)
            .values(extracted_bytes_consumed=IngestionBatch.extracted_bytes_consumed - amount)
        )
        self.db.commit()

    def _reconcile_extracted_bytes(self, *, batch_id: int, estimated_bytes: int, actual_bytes: int) -> None:
        """Post-extraction reconciliation (Milestone 5 design, section
        6): a plain, unconditional correction - the admission decision
        already happened in `_preflight_admit`; this only makes the
        reported total accurate to what was REALLY written, across the
        whole recursive tree, summed once."""
        delta = actual_bytes - estimated_bytes
        if delta == 0:
            return
        self.db.execute(
            IngestionBatch.__table__.update()
            .where(IngestionBatch.id == batch_id)
            .values(extracted_bytes_consumed=IngestionBatch.extracted_bytes_consumed + delta)
        )
        self.db.commit()

    def _process_claimed_archive(
        self,
        instance: SourceInstance,
        *,
        worker_id: str,
        workspace_root: Path,
        max_depth: int,
        batch: IngestionBatch | None,
        estimated_bytes: int,
    ) -> None:
        archive_path = Path(instance.root_t7_path)
        root_step = ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path=instance.root_t7_path)
        accumulator = _ByteAccumulator()

        try:
            self._extract_recursive(
                archive_path=archive_path,
                classification_run_id=instance.classification_run_id,
                root_t7_path=instance.root_t7_path,
                root_instance_id=instance.id,
                chain_prefix=[root_step],
                member_path_prefix="",
                depth=0,
                max_depth=max_depth,
                workspace_root=workspace_root,
                accumulator=accumulator,
            )
        except Exception as exc:  # noqa: BLE001 - classified below, then re-raised as a durable attempt
            if batch is not None and batch.max_extracted_bytes is not None:
                self._release_extracted_bytes_reservation(batch_id=batch.id, amount=estimated_bytes)
            self.attempts.record_identity_resolution_attempt(
                source_instance_id=instance.id,
                worker_id=worker_id,
                outcome=IngestionAttemptOutcome.FAILED,
                failure_code=_classify_extraction_failure(exc),
                failure_detail=str(exc),
                retryable=_is_extraction_retryable(exc),
            )
            return

        if batch is not None and batch.max_extracted_bytes is not None:
            self._reconcile_extracted_bytes(
                batch_id=batch.id, estimated_bytes=estimated_bytes, actual_bytes=accumulator.total_bytes
            )

        # risk_tier_actual is deliberately NOT populated here - see
        # SourceInstance's own docstring: no numeric mapping from
        # (member count, max depth reached, measured expansion ratio)
        # to LOW/MEDIUM/HIGH/EXTREME has ever been frozen anywhere in
        # this design chain, and inventing one now would violate the
        # explicit "do not invent the deferred numeric thresholds"
        # instruction. The column exists (nullable, write-once) for a
        # future, separately-authorized calibration pass to populate.

        self.attempts.record_identity_resolution_attempt(
            source_instance_id=instance.id,
            worker_id=worker_id,
            outcome=IngestionAttemptOutcome.SUCCEEDED,
        )

    def _extract_recursive(
        self,
        *,
        archive_path: Path,
        classification_run_id: int,
        root_t7_path: str,
        root_instance_id: int,
        chain_prefix: list[ProvenanceStep],
        member_path_prefix: str,
        depth: int,
        max_depth: int,
        workspace_root: Path,
        accumulator: _ByteAccumulator,
    ) -> None:
        if depth >= max_depth:
            raise ValueError(
                f"archive nesting exceeds max_depth={max_depth} at {archive_path}"
            )

        archive_file = DiscoveredFile(
            path=archive_path,
            relative_path=Path(archive_path.name),
            size=archive_path.stat().st_size,
        )
        destination = (
            _staging_root(workspace_root, root_instance_id)
            if depth == 0
            else Path(_nested_staging_dir(workspace_root, root_instance_id, member_path_prefix))
        )
        extracted = self.extractor.extract([archive_file], destination)
        members = [f for f in extracted if f.path != archive_file.path]
        accumulator.add(sum(m.size for m in members))

        for member in members:
            member_relative = member.relative_path.as_posix()
            full_member_path = (
                member_relative
                if not member_path_prefix
                else f"{member_path_prefix}/{member_relative}"
            )

            step = ProvenanceStep(kind=ProvenanceLinkKind.ARCHIVE_MEMBER, path=member_relative)
            full_chain = [*chain_prefix, step]

            existing = self._find_existing_member(
                classification_run_id, root_t7_path, full_member_path
            )
            if existing is None:
                member_source_category = classify_source_category(
                    root_t7_path=root_t7_path, member_path=full_member_path
                )
                member_workload_category = classify_workload_category(
                    root_t7_path=root_t7_path,
                    member_path=full_member_path,
                    source_category=member_source_category,
                )
                member_risk_tier_estimated = classify_risk_tier_estimated(
                    source_category=member_source_category, declared_size_bytes=member.size
                )
                member_instance = self.instances.create_instance(
                    classification_run_id=classification_run_id,
                    root_t7_path=root_t7_path,
                    member_path=full_member_path,
                    evidence_snapshot={
                        "discovered_during_extraction": True,
                        "parent_source_instance_id": root_instance_id,
                    },
                    chain=full_chain,
                    source_category=member_source_category,
                    workload_category=member_workload_category,
                    risk_tier_estimated=member_risk_tier_estimated,
                )
            else:
                member_instance = existing

            if member.path.suffix.lower() in _ARCHIVE_SUFFIXES:
                self._extract_recursive(
                    archive_path=member.path,
                    classification_run_id=classification_run_id,
                    root_t7_path=root_t7_path,
                    root_instance_id=root_instance_id,
                    chain_prefix=full_chain,
                    member_path_prefix=full_member_path,
                    depth=depth + 1,
                    max_depth=max_depth,
                    workspace_root=workspace_root,
                    accumulator=accumulator,
                )
                continue

            if existing is not None and existing.content_identity_group_id is not None:
                # Already fully identity-resolved by an earlier
                # (possibly crashed-and-resumed) attempt - idempotent
                # skip, never re-hashed or re-written.
                continue

            content = member.path.read_bytes()
            identity_hash = hashlib.sha256(content).hexdigest()
            eligibility = classify_eligibility(member.path)
            initial_state = initial_pipeline_state_for_eligibility(eligibility)

            group = self.identity.get_or_create_group(
                identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
                identity_algorithm=ContentIdentityAlgorithm.SHA256,
                identity_hash=identity_hash,
                initial_pipeline_state=initial_state,
            )

            if eligibility == IngestionEligibility.ELIGIBLE:
                write_workspace_content(workspace_root, group.id, content, suffix=member.path.suffix)

            self.identity.assign_content_identity(member_instance.id, group)

    def _find_existing_member(
        self,
        classification_run_id: int,
        root_t7_path: str,
        member_path: str,
    ) -> SourceInstance | None:
        return self.db.execute(
            select(SourceInstance).where(
                SourceInstance.classification_run_id == classification_run_id,
                SourceInstance.root_t7_path == root_t7_path,
                SourceInstance.member_path == member_path,
            )
        ).scalar_one_or_none()


def _is_extraction_retryable(exc: Exception) -> bool:
    """`max_depth` exceeded is a DETERMINISTIC property of this specific
    archive's own structure against a fixed configuration value -
    retrying with the SAME max_depth against the SAME archive fails
    identically, every time (Milestone 5 design, section 13's frozen
    outcome). Every other archive failure category's retryable
    semantics are unchanged from before this milestone."""
    if isinstance(exc, ValueError) and _is_max_depth_exceeded_message(str(exc).lower()):
        return False
    return True


def _classify_extraction_failure(exc: Exception) -> IngestionFailureCode:
    if isinstance(exc, PermissionError):
        return IngestionFailureCode.PERMISSION_DENIED
    if isinstance(exc, FileNotFoundError):
        return IngestionFailureCode.T7_UNAVAILABLE
    if isinstance(exc, OSError):
        return IngestionFailureCode.INSUFFICIENT_DISK_SPACE
    if isinstance(exc, ValueError):
        message = str(exc).lower()
        if "expansion ratio" in message:
            return IngestionFailureCode.OVERSIZED_OR_EXPANSION_LIMIT
        if _is_max_depth_exceeded_message(message):
            # Frozen outcome (Milestone 5 design, section 13): grouped
            # with the expansion-ratio bomb guard as the SAME category
            # of deliberate hard structural limit - never MALFORMED_
            # ARCHIVE (a too-deeply-nested archive is not unreadable or
            # corrupt). A dedicated new enum value would be more
            # precise but requires a migration (ALTER TYPE ... ADD
            # VALUE), forbidden by this milestone's boundaries.
            return IngestionFailureCode.OVERSIZED_OR_EXPANSION_LIMIT
        return IngestionFailureCode.MALFORMED_ARCHIVE
    return IngestionFailureCode.EXTRACTION_ERROR_OTHER

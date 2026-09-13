from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classification.content_identity_service import ContentIdentityService
from app.classification.eligibility_service import IngestionEligibility, classify_eligibility
from app.classification.ingestion_attempt_service import IngestionAttemptService
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
from app.models.provenance_link import ProvenanceLinkKind
from app.models.source_instance import SourceInstance

_ARCHIVE_SUFFIXES = (".zip", ".7z")


class ArchiveProcessingService:
    """Recursively processes ONE claimed, top-level archive
    SourceInstance: opens it, enumerates members (reusing the existing,
    already-tested `ArchiveExtractor` and its safety guards - path-
    traversal rejection, expansion-ratio bomb guard, disk-space
    reserve, symlink-member rejection - unmodified), creates a
    SourceInstance for every member (including nested archives, which
    are themselves recursed into up to `max_depth`), and resolves the
    content identity of every LEAF (non-archive) member immediately -
    per the frozen design, a member's identity is only ever knowable
    AFTER it has been read, which extraction just did.

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
    ) -> SourceInstance | None:
        instance = self.claims.claim_source_instance_for_archive_processing(
            worker_id=worker_id, lease_duration=lease_duration
        )
        if instance is None:
            return None

        try:
            self._process_claimed_archive(
                instance,
                worker_id=worker_id,
                workspace_root=workspace_root,
                max_depth=max_depth,
            )
        finally:
            self.claims.release_source_instance_claim(instance.id)

        self.db.refresh(instance)
        return instance

    def _process_claimed_archive(
        self,
        instance: SourceInstance,
        *,
        worker_id: str,
        workspace_root: Path,
        max_depth: int,
    ) -> None:
        archive_path = Path(instance.root_t7_path)
        root_step = ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path=instance.root_t7_path)

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
            )
        except Exception as exc:  # noqa: BLE001 - classified below, then re-raised as a durable attempt
            self.attempts.record_identity_resolution_attempt(
                source_instance_id=instance.id,
                worker_id=worker_id,
                outcome=IngestionAttemptOutcome.FAILED,
                failure_code=_classify_extraction_failure(exc),
                failure_detail=str(exc),
                retryable=True,
            )
            return

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
        destination = workspace_root / f"archive_{root_instance_id}" / f"depth_{depth}_{archive_path.stem}"
        extracted = self.extractor.extract([archive_file], destination)
        members = [f for f in extracted if f.path != archive_file.path]

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
                member_instance = self.instances.create_instance(
                    classification_run_id=classification_run_id,
                    root_t7_path=root_t7_path,
                    member_path=full_member_path,
                    evidence_snapshot={
                        "discovered_during_extraction": True,
                        "parent_source_instance_id": root_instance_id,
                    },
                    chain=full_chain,
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


def _classify_extraction_failure(exc: Exception) -> IngestionFailureCode:
    if isinstance(exc, PermissionError):
        return IngestionFailureCode.PERMISSION_DENIED
    if isinstance(exc, FileNotFoundError):
        return IngestionFailureCode.T7_UNAVAILABLE
    if isinstance(exc, OSError):
        return IngestionFailureCode.INSUFFICIENT_DISK_SPACE
    if isinstance(exc, ValueError):
        if "expansion ratio" in str(exc).lower():
            return IngestionFailureCode.OVERSIZED_OR_EXPANSION_LIMIT
        return IngestionFailureCode.MALFORMED_ARCHIVE
    return IngestionFailureCode.EXTRACTION_ERROR_OTHER

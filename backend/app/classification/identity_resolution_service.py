from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.classification.content_identity_service import ContentIdentityService
from app.classification.eligibility_service import IngestionEligibility, classify_eligibility
from app.classification.ingestion_attempt_service import IngestionAttemptService
from app.classification.worker_claim_service import WorkerClaimService
from app.classification.workspace import (
    initial_pipeline_state_for_eligibility,
    write_workspace_content,
)
from app.models.content_identity_group import ContentIdentityAlgorithm, ContentIdentityKind
from app.models.ingestion_attempt import IngestionAttemptOutcome, IngestionFailureCode
from app.models.source_instance import SourceInstance


class IdentityResolutionService:
    """Resolves the content identity of a root-level SourceInstance
    whose `content_identity_group_id` is NULL - a uniquely-sized loose
    file D1 never hashed (the deferred-identity case established in
    `14b8063` round 5, corrected there to NOT be archive-member-
    exclusive).

    Reads `SourceInstance.root_t7_path` exactly ONCE, to compute this
    occurrence's content-identity hash - this is the one legitimate
    read of that field this design always intended (see `6491dad`'s
    Q4: "identity is unknown until the ingestion pipeline itself reads
    and hashes the file"). It is never re-read afterward to "re-verify"
    already-captured evidence - that principle is about not treating
    `evidence_snapshot` as something to re-validate against a live
    re-read, not about forbidding the one read identity resolution
    exists to perform.

    In this implementation gate, `root_t7_path` is ALWAYS a synthetic
    path under a test's own tmp_path, standing in for what would be a
    real T7 path in production - no code here is T7-specific, and
    nothing in this module ever references either real T7 mount point.
    """

    def __init__(self, db: Session):
        self.db = db
        self.claims = WorkerClaimService(db)
        self.identity = ContentIdentityService(db)
        self.attempts = IngestionAttemptService(db)

    def resolve_next(
        self,
        *,
        worker_id: str,
        workspace_root: Path,
        lease_duration: timedelta = timedelta(minutes=10),
    ) -> SourceInstance | None:
        """Claims and resolves ONE unresolved root-level SourceInstance.
        Returns the (now identity-resolved, or durably failed) instance,
        or None if no eligible work exists.
        """
        instance = self.claims.claim_source_instance_for_identity_resolution(
            worker_id=worker_id, lease_duration=lease_duration
        )
        if instance is None:
            return None

        try:
            self._resolve_claimed_instance(instance, worker_id=worker_id, workspace_root=workspace_root)
        finally:
            # Always release the claim - on success content_identity_
            # group_id is already set (write-once, unaffected by
            # releasing claimed_by/claimed_at); on failure the instance
            # remains unresolved and eligible for a future retry claim.
            # Fenced to the generation this call itself was granted
            # (Milestone 5) - a safe no-op if a stale-claim recovery has
            # since reclaimed this row for a different worker.
            self.claims.release_source_instance_claim(instance.id, claim_generation=instance.claim_generation)

        self.db.refresh(instance)
        return instance

    def _resolve_claimed_instance(
        self,
        instance: SourceInstance,
        *,
        worker_id: str,
        workspace_root: Path,
    ) -> None:
        source_path = Path(instance.root_t7_path)

        try:
            content = source_path.read_bytes()
        except FileNotFoundError:
            self.attempts.record_identity_resolution_attempt(
                source_instance_id=instance.id,
                worker_id=worker_id,
                outcome=IngestionAttemptOutcome.FAILED,
                failure_code=IngestionFailureCode.T7_UNAVAILABLE,
                failure_detail=f"source path not found: {source_path}",
                retryable=True,
            )
            return
        except PermissionError:
            self.attempts.record_identity_resolution_attempt(
                source_instance_id=instance.id,
                worker_id=worker_id,
                outcome=IngestionAttemptOutcome.FAILED,
                failure_code=IngestionFailureCode.PERMISSION_DENIED,
                failure_detail=f"permission denied reading: {source_path}",
                retryable=True,
            )
            return
        except OSError as exc:
            self.attempts.record_identity_resolution_attempt(
                source_instance_id=instance.id,
                worker_id=worker_id,
                outcome=IngestionAttemptOutcome.FAILED,
                failure_code=IngestionFailureCode.READ_ERROR_OTHER,
                failure_detail=str(exc),
                retryable=True,
            )
            return

        identity_hash = hashlib.sha256(content).hexdigest()

        eligibility = classify_eligibility(source_path)
        initial_state = initial_pipeline_state_for_eligibility(eligibility)

        group = self.identity.get_or_create_group(
            identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
            identity_algorithm=ContentIdentityAlgorithm.SHA256,
            identity_hash=identity_hash,
            initial_pipeline_state=initial_state,
        )

        if eligibility == IngestionEligibility.ELIGIBLE:
            write_workspace_content(workspace_root, group.id, content, suffix=source_path.suffix)

        self.identity.assign_content_identity(instance.id, group)

        self.attempts.record_identity_resolution_attempt(
            source_instance_id=instance.id,
            worker_id=worker_id,
            outcome=IngestionAttemptOutcome.SUCCEEDED,
        )

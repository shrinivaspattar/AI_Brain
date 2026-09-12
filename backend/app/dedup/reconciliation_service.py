from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dedup.execution_plan_service import _observe_file
from app.models.dedup_execution import (
    DedupExecutionActionAudit,
    DedupExecutionActionReconciliation,
    DedupExecutionActionResult,
)

_RECONCILABLE_RESULTS = frozenset(
    {DedupExecutionActionResult.SUCCESS, DedupExecutionActionResult.FAILED}
)


class DedupReconciliationService:
    """Records a human's later, independently-verified finding about
    one `UNKNOWN` `DedupExecutionActionAudit` row - see "Executor
    Reconciliation & TOCTOU Strategy" in AI_Brain_Architecture.md for
    the full design this implements.

    This service performs NO filesystem mutation of its own. It reads
    the filesystem exactly once per call, via the same non-mutating
    `_observe_file` the executor and `recover_stale_execution` already
    use, purely to CORROBORATE a human's claim before recording it -
    never to act on what it finds. A claim inconsistent with that fresh
    observation is refused; the original audit row is never edited, and
    `DedupExecution.status` is never touched by this service at all.

    Not locked against a concurrent second reconciliation of the same
    audit beyond the database's own unique constraint on `audit_id`
    (`uq_dedup_execution_action_reconciliations_audit_id`): unlike
    execution-claiming or recovery-claiming, reconciliation is a rare,
    deliberate, single-human action, not a systemic concurrency
    surface, and adding `SELECT ... FOR UPDATE` here was not part of
    this milestone's scope. A genuine race would surface as a raw
    `IntegrityError` from that constraint rather than a clean
    `ValueError` - a known, accepted residual, not silently unhandled.
    """

    def __init__(self, db: Session):
        self.db = db

    def reconcile_action(
        self,
        audit_id: int,
        verified_result: DedupExecutionActionResult,
        verified_by: str,
        verification_method: str,
    ) -> DedupExecutionActionReconciliation:
        """Reconcile one `UNKNOWN` audit into a definite `SUCCESS` or
        `FAILED` outcome, corroborated by a fresh, independent
        re-observation of the audit's own `source_path` taken right
        now - not by trusting `verified_by`/`verification_method`
        alone.

        Preconditions (`ValueError`, no row created on failure):
        1. `verified_result` must be exactly `SUCCESS` or `FAILED` -
           reconciling to `UNKNOWN` would be a no-op, and
           `PRECONDITION_FAILED`/`NOT_ATTEMPTED` are never reachable
           outcomes for an audit that was already `UNKNOWN`.
        2. The audit must exist.
        3. The audit's `result` must currently be `UNKNOWN` - a
           definite result never needs reconciling.
        4. No reconciliation may already exist for this audit (also
           enforced by a database unique constraint) - an audit is
           reconciled exactly once, permanently.
        5. The fresh observation of `audit.source_path` must be
           CONSISTENT with the claimed `verified_result`:
           - `SUCCESS` requires the source path to no longer exist -
             a delete genuinely cannot have happened if the file is
             still there.
           - `FAILED` requires the source path to still exist, with a
             hash and size matching `audit.expected_content_hash`/
             `expected_file_size` (the plan's original, pre-mutation
             expected values) - "nothing happened" requires the
             original file to still be exactly what it was.
           A claim inconsistent with what the system can itself
           observe right now is refused outright, never recorded.

        The observation's own hash/size (whatever it found - including
        `None`/`None` for a confirmed-absent file under `SUCCESS`) is
        stored on the reconciliation row as `observed_content_hash`/
        `observed_file_size`, independent of anything the human
        reported in `verification_method`.
        """
        if verified_result not in _RECONCILABLE_RESULTS:
            raise ValueError(
                f"verified_result must be SUCCESS or FAILED, not "
                f"{verified_result.value} - reconciliation exists to resolve an "
                "indeterminate outcome into a definite one, never into another "
                "indeterminate or not-applicable result"
            )

        audit = self.db.get(DedupExecutionActionAudit, audit_id)
        if audit is None:
            raise ValueError(f"Dedup execution action audit {audit_id} not found")

        if audit.result != DedupExecutionActionResult.UNKNOWN:
            raise ValueError(
                f"Audit {audit_id} has result={audit.result.value}, not UNKNOWN - "
                "only an indeterminate outcome is eligible for reconciliation"
            )

        existing = self.get_reconciliation(audit_id)
        if existing is not None:
            raise ValueError(
                f"Audit {audit_id} already has a reconciliation (id "
                f"{existing.id}) - an audit is reconciled exactly once, "
                "permanently"
            )

        observation = _observe_file(audit.source_path)

        if verified_result == DedupExecutionActionResult.SUCCESS:
            if observation.exists:
                raise ValueError(
                    f"Cannot reconcile audit {audit_id} as SUCCESS - source path "
                    f"{audit.source_path} still exists right now, which is "
                    "inconsistent with a delete having genuinely occurred; a "
                    "reconciliation claim must agree with what the system can "
                    "itself observe, not be recorded on the human's word alone"
                )
        else:  # FAILED
            if not observation.exists:
                raise ValueError(
                    f"Cannot reconcile audit {audit_id} as FAILED - source path "
                    f"{audit.source_path} no longer exists, which is inconsistent "
                    "with 'nothing happened to the original file'; a "
                    "reconciliation claim must agree with what the system can "
                    "itself observe, not be recorded on the human's word alone"
                )
            if (
                observation.content_hash != audit.expected_content_hash
                or observation.file_size != audit.expected_file_size
            ):
                raise ValueError(
                    f"Cannot reconcile audit {audit_id} as FAILED - source path "
                    f"{audit.source_path} exists but no longer matches the "
                    "plan's original expected hash/size, which is inconsistent "
                    "with 'nothing happened to the original file'"
                )

        reconciliation = DedupExecutionActionReconciliation(
            audit_id=audit_id,
            verified_result=verified_result,
            verified_by=verified_by,
            verification_method=verification_method,
            observed_content_hash=observation.content_hash,
            observed_file_size=observation.file_size,
        )
        self.db.add(reconciliation)

        try:
            self.db.commit()
            self.db.refresh(reconciliation)
            return reconciliation
        except Exception:
            self.db.rollback()
            raise

    def get_reconciliation(
        self, audit_id: int
    ) -> DedupExecutionActionReconciliation | None:
        return self.db.scalar(
            select(DedupExecutionActionReconciliation).where(
                DedupExecutionActionReconciliation.audit_id == audit_id
            )
        )

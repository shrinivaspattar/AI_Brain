from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.source_instance import CanonicalStatus, SourceInstance


class CanonicalDecisionService:
    """Records a canonical-status decision for a SourceInstance.

    Canonicality is a relationship between a SourceInstance and the
    ContentIdentityGroup it belongs to, never an intrinsic, global
    property of a file - this service only ever changes the status of
    ONE SourceInstance row, and never touches any sibling instance in
    the same group (marking one CANONICAL does NOT imply marking
    others NON_CANONICAL - that requires its own separate, explicit
    call with its own evidence).

    `reason`, `decided_by`, and `decided_at` are REQUIRED for both
    CANONICAL and NON_CANONICAL (enforced additionally by a DB CHECK
    constraint on source_instances) - this service's signature makes
    that requirement impossible to bypass by accident from Python, and
    the DB constraint catches any other write path that might try.
    """

    def __init__(self, db: Session):
        self.db = db

    def decide(
        self,
        instance: SourceInstance,
        *,
        status: CanonicalStatus,
        reason: str,
        decided_by: str,
    ) -> SourceInstance:
        if status == CanonicalStatus.UNRESOLVED:
            raise ValueError(
                "decide() sets an explicit decision (CANONICAL or "
                "NON_CANONICAL); UNRESOLVED is the default, never an "
                "explicit decision to make."
            )
        if not reason or not decided_by:
            raise ValueError(
                "A canonical-status decision requires both a reason and "
                "a decided_by - never inferred, never silent."
            )

        instance.canonical_status = status
        instance.canonical_status_reason = reason
        instance.canonical_status_decided_by = decided_by
        instance.canonical_status_decided_at = datetime.now(UTC)

        self.db.commit()
        self.db.refresh(instance)
        return instance

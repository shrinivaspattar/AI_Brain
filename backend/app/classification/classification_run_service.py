from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.classification_run import ClassificationRun


class ClassificationRunService:
    """Creates a ClassificationRun referencing the D0/D1/D2 DiscoveryRun
    rows it consumed. Never touches the T7 - this only records that a
    classification pass happened and which already-recorded discovery
    reports it read.
    """

    def __init__(self, db: Session):
        self.db = db

    def start_run(
        self,
        *,
        classifier_version: str,
        d0_discovery_run_id: int | None = None,
        d1_discovery_run_id: int | None = None,
        d2_discovery_run_id: int | None = None,
    ) -> ClassificationRun:
        if (
            d0_discovery_run_id is None
            and d1_discovery_run_id is None
            and d2_discovery_run_id is None
        ):
            raise ValueError(
                "A ClassificationRun must reference at least one DiscoveryRun."
            )

        run = ClassificationRun(
            classifier_version=classifier_version,
            d0_discovery_run_id=d0_discovery_run_id,
            d1_discovery_run_id=d1_discovery_run_id,
            d2_discovery_run_id=d2_discovery_run_id,
            started_at=datetime.now(UTC),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def complete_run(self, run: ClassificationRun) -> ClassificationRun:
        run.completed_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(run)
        return run

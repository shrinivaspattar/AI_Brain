from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ClassificationRun(Base):
    """One execution of the classification stage: reads already-produced
    D0/D1/D2 DiscoveryRun reports and decides, per path, what to ingest -
    ingest-canonical / duplicate-instance-only / quarantine / needs-
    human-review - before a single archive is ever opened. Zero T7
    access of any kind.

    Re-classification is expected and first-class, mirroring D2's own
    `load_raw_from_previous_report` philosophy of reprocessing without
    re-touching the T7 - a new ClassificationRun is created each time,
    never an edit to an old one.

    `classifier_version` identifies WHICH classification behavior
    produced this run's SourceInstance rows - a lightweight, immutable
    implementation identifier. Without it, two ClassificationRuns over
    the identical D2 report could disagree in their resulting
    classification and nothing durable would explain why. This is
    deliberately NOT a full Processor/ProcessorVersion architecture -
    that remains a dormant future backlog item.
    """

    __tablename__ = "classification_runs"
    __table_args__ = (
        CheckConstraint(
            "d0_discovery_run_id IS NOT NULL "
            "OR d1_discovery_run_id IS NOT NULL "
            "OR d2_discovery_run_id IS NOT NULL",
            name="ck_classification_runs_at_least_one_discovery_run",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    # Immutable identifier for the classification logic that ran - the
    # durable answer to "why did this run classify differently from
    # that one over the same report."
    classifier_version: Mapped[str] = mapped_column(String(100), nullable=False)

    d0_discovery_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("discovery_runs.id"),
        nullable=True,
        index=True,
    )
    d1_discovery_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("discovery_runs.id"),
        nullable=True,
        index=True,
    )
    d2_discovery_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("discovery_runs.id"),
        nullable=True,
        index=True,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    # Deliberately no updated_at: fully immutable once written.

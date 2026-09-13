from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class DiscoveryRunKind(str, Enum):
    D0_INVENTORY = "d0_inventory"
    D1_DUPLICATE_ANALYSIS = "d1_duplicate_analysis"
    D2_PROVENANCE_ANALYSIS = "d2_provenance_analysis"


class DiscoveryRun(Base):
    """One row per an already-completed T7 discovery run (D0/D1/D2).

    Deliberately NOT the heavier, atomic `CorpusSnapshot` concept from
    the dormant backlog: D0/D1/D2 are three separate, sequential,
    non-atomic runs against a live corpus (D2 ran ~2 hours after D1,
    with Syncthing active throughout both). This table does not pretend
    otherwise - it only gives each already-real run a durable,
    referenceable identity.

    INVARIANT: a DiscoveryRun identifies an observed analysis/report,
    NOT a transactionally consistent filesystem snapshot. `report_sha256`
    proves which exact report artifact a ClassificationRun consumed -
    it proves nothing about whether the T7 filesystem itself was
    unchanging while that report was being produced, and no code may
    ever read it as such.

    Creating a row here never touches the T7 - it only records metadata
    about a report file (produced by app/discovery/*.py) that already
    exists on disk.
    """

    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        index=True,
    )

    run_kind: Mapped[DiscoveryRunKind] = mapped_column(
        SQLEnum(DiscoveryRunKind, name="discovery_run_kind"),
        nullable=False,
    )

    # The T7 root as recorded by the discovery tool - inert metadata,
    # never dereferenced again as a live filesystem path by this model.
    source_root: Mapped[str] = mapped_column(Text, nullable=False)

    # SHA-256 of the report JSON file itself. Proves WHICH REPORT was
    # consumed by a later ClassificationRun - never that the T7 was
    # quiescent while producing it (see class docstring).
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    run_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    run_completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    # Deliberately no updated_at: every field on this row is fully
    # immutable once written - a DiscoveryRun is an audit record of an
    # event that already happened, never a live/mutable job.

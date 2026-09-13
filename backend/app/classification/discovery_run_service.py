import hashlib
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind

_HASH_READ_CHUNK_SIZE = 1024 * 1024


class DiscoveryRunService:
    """Records a DiscoveryRun for an already-completed D0/D1/D2 report.

    Never touches the T7: `report_path` names a report JSON file that
    `app/discovery/*.py` has already produced and written to disk (e.g.
    under `knowledge/t7_discovery/`, gitignored). This service only
    reads THAT file, to compute its hash - it performs no filesystem
    scan, no os.walk, no os.lstat of anything under a T7 mount point.
    """

    def __init__(self, db: Session):
        self.db = db

    def record_run(
        self,
        *,
        run_kind: DiscoveryRunKind,
        source_root: str,
        report_path: Path,
        run_started_at: datetime,
        run_completed_at: datetime,
    ) -> DiscoveryRun:
        report_sha256 = _hash_report_file(report_path)

        run = DiscoveryRun(
            run_kind=run_kind,
            source_root=source_root,
            report_sha256=report_sha256,
            run_started_at=run_started_at,
            run_completed_at=run_completed_at,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run


def _hash_report_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_HASH_READ_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()

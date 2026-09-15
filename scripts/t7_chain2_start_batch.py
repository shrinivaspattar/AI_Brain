#!/usr/bin/env python3
"""One-off helper: BatchControlService.start(batch_id) against
production aibrain, avoiding an inline python -c multi-line block
(which is easy to garble via copy-paste in a terminal).

Usage:
    python scripts/t7_chain2_start_batch.py <ingestion_batch_id>
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.classification.batch_control_service import BatchControlService  # noqa: E402
from app.core.config import settings  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python t7_chain2_start_batch.py <ingestion_batch_id>")
    batch_id = int(sys.argv[1])

    engine = create_engine(make_url(settings.DATABASE_URL).set(database="aibrain"))
    with Session(engine) as db:
        result = BatchControlService(db).start(batch_id)
        print(f"applied={result.applied} status={result.batch.status.value}")


if __name__ == "__main__":
    main()

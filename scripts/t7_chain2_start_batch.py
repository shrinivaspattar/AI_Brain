#!/usr/bin/env python3
"""One-off helper: BatchControlService.start(batch_id) (PLANNED -> RUNNING),
avoiding an inline python -c multi-line block (which is easy to garble via
copy-paste in a terminal).

DATABASE SAFETY: defaults to `aibrain_test`, like run_ingestion_batch.py.
Production needs `--database aibrain` explicitly (earlier versions of this
helper always used production).

Usage:
    python scripts/t7_chain2_start_batch.py <ingestion_batch_id> [--database NAME]
"""

import argparse
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("batch_id", type=int)
    parser.add_argument("--database", default="aibrain_test")
    args = parser.parse_args()

    engine = create_engine(make_url(settings.DATABASE_URL).set(database=args.database))
    print(f"Using database: {args.database}")
    with Session(engine) as db:
        result = BatchControlService(db).start(args.batch_id)
        print(f"applied={result.applied} status={result.batch.status.value}")


if __name__ == "__main__":
    main()

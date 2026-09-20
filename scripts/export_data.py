#!/usr/bin/env python3
"""Export conversations, messages and memories to plain JSON files, or
restore them into an EMPTY database (decision 0001: files are the source of
truth; these rows otherwise exist only in Postgres).

Each export is a new timestamped snapshot directory; nothing is overwritten.
The default location, documents/exports/, is gitignored - these files contain
your private chat history and memories, so never commit them.

Usage:
    python scripts/export_data.py export [--out-dir DIR] [--database NAME]
    python scripts/export_data.py restore SNAPSHOT_DIR --database NAME

`export` only reads the database. `restore` writes, refuses unless the
conversations, messages and memories tables are all empty, preserves ids, and
runs as a single transaction.
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.export.service import DataExportService  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export")
    export.add_argument("--out-dir", type=Path, default=REPO_ROOT / "documents" / "exports")
    export.add_argument("--database", default="aibrain")

    restore = sub.add_parser("restore")
    restore.add_argument("snapshot_dir", type=Path)
    restore.add_argument("--database", required=True, help="explicit on purpose: restore writes")

    args = parser.parse_args()
    engine = create_engine(make_url(settings.DATABASE_URL).set(database=args.database))

    with Session(engine) as db:
        service = DataExportService(db)
        if args.command == "export":
            result = service.export(args.out_dir)
            verb = "Exported"
        else:
            try:
                result = service.restore(args.snapshot_dir)
            except ValueError as exc:
                raise SystemExit(f"Restore refused: {exc}")
            verb = "Restored"

    print(f"{verb} {result.conversations} conversations, {result.messages} messages, "
          f"{result.memories} memories -> {result.snapshot_dir}")


if __name__ == "__main__":
    main()

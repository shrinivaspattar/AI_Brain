#!/usr/bin/env python3
"""Take documents out of the search index by ORIGINAL FILE NAME (for example
account recovery keys). For each matching content group it deletes the
document's chunks and the document row, and marks the group EXCLUDED (the
existing "deliberately out of scope by policy" state), so it is never chunked
or embedded again. Optionally it also deletes the group's working copy under
--workspace-root. It never touches the original files on any drive.

DATABASE SAFETY: defaults to `aibrain_pilot`; any other database needs
--allow-other-db. Use --dry-run first.

Usage:
    python scripts/exclude_from_index.py --name-contains "recovery key" \\
        [--name-contains ...] [--workspace-root documents/workspace_priority] [--dry-run]
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, delete, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models.ingestion_batch import IngestionBatch  # noqa: E402,F401  (the mapper needs every referenced table registered)
from app.core.config import settings  # noqa: E402
from app.models.content_identity_group import ContentIdentityGroup, ContentPipelineState  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.document_chunk import DocumentChunk  # noqa: E402
from app.models.source_instance import SourceInstance  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name-contains", action="append", required=True, help="case-insensitive text in the original file name")
    ap.add_argument("--database", default="aibrain_pilot")
    ap.add_argument("--allow-other-db", action="store_true")
    ap.add_argument("--workspace-root", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.database != "aibrain_pilot" and not a.allow_other_db:
        raise SystemExit("refusing another database without --allow-other-db")
    needles = [n.lower() for n in a.name_contains]

    engine = create_engine(make_url(settings.DATABASE_URL).set(database=a.database))
    with Session(engine) as db:
        rows = db.execute(select(SourceInstance.content_identity_group_id, SourceInstance.root_t7_path,
                                 SourceInstance.member_path)
                          .where(SourceInstance.content_identity_group_id.is_not(None))).all()
        groups: dict[int, str] = {}
        for gid, root, member in rows:
            name = os.path.basename(member or root).lower()
            if any(n in name for n in needles):
                groups[gid] = os.path.basename(member or root)
        print(f"database {a.database}: {len(groups)} matching content group(s)")
        for gid, name in sorted(groups.items()):
            print(f"  group {gid}: {name}")
        if a.dry_run or not groups:
            print("DRY RUN - nothing changed." if a.dry_run else "Nothing to do.")
            return
        for gid in groups:
            doc_ids = [d for (d,) in db.execute(select(Document.id).where(Document.content_identity_group_id == gid))]
            if doc_ids:
                db.execute(delete(DocumentChunk).where(DocumentChunk.document_id.in_(doc_ids)))
                db.execute(delete(Document).where(Document.id.in_(doc_ids)))
            group = db.get(ContentIdentityGroup, gid)
            group.pipeline_state = ContentPipelineState.EXCLUDED
        db.commit()
        removed_copies = 0
        if a.workspace_root:
            for gid in groups:
                folder = a.workspace_root / f"group_{gid}"
                if folder.is_dir():
                    shutil.rmtree(folder)
                    removed_copies += 1
        print(f"excluded {len(groups)} group(s); removed {removed_copies} working cop(ies).")


if __name__ == "__main__":
    main()

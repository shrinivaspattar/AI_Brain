#!/usr/bin/env python3
"""Repair Chain 2 documents whose `source` points at a workspace copy that no
longer exists (e.g. a workspace under /tmp that a reboot removed).

For each such document this finds one of its original occurrences on the
source drive, VERIFIES the bytes by SHA-256 against the content-identity hash
recorded when the document was ingested, writes a fresh workspace copy under a
persistent root, and points `Document.source` at it. Nothing on the source
drive is written to; the drive must be mounted. Documents with no verified
reachable original are reported and left untouched.

DRY RUN by default: it reads the database and the drive and prints what it
would do. `--apply` writes the workspace copies and updates the database.

Usage:
    python scripts/repair_chain2_document_sources.py \\
        --workspace-root documents/workspace            # dry run
    python scripts/repair_chain2_document_sources.py \\
        --workspace-root documents/workspace --apply
"""

import argparse
import hashlib
import importlib
import os
import pkgutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import app.models  # noqa: E402
from app.classification.workspace import workspace_content_path  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.models.content_identity_group import ContentIdentityGroup  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.source_instance import SourceInstance  # noqa: E402

for _module in pkgutil.iter_modules(app.models.__path__):
    importlib.import_module(f"app.models.{_module.name}")


def find_verified_original(db: Session, group: ContentIdentityGroup) -> tuple[Path, bytes] | tuple[None, str]:
    instances = db.scalars(
        select(SourceInstance)
        .where(SourceInstance.content_identity_group_id == group.id, SourceInstance.member_path.is_(None))
        .order_by(SourceInstance.id)
    ).all()
    if not instances:
        return None, "no loose-file occurrence recorded (archive members are not repaired here)"
    reasons = []
    for instance in instances:
        path = Path(instance.root_t7_path)
        if not path.is_file():
            reasons.append("original not reachable (drive unmounted or file moved)")
            continue
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != group.identity_hash:
            reasons.append("original exists but its hash no longer matches the recorded identity")
            continue
        return path, content
    return None, "; ".join(sorted(set(reasons)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--database", default="aibrain")
    parser.add_argument("--apply", action="store_true", help="write workspace copies and update the database")
    parser.add_argument("--allow-tmp", action="store_true", help="permit a workspace root under /tmp (it is wiped on reboot)")
    args = parser.parse_args()

    root = args.workspace_root.resolve()
    if not args.allow_tmp and (root == Path("/tmp") or Path("/tmp") in root.parents):
        raise SystemExit(f"Refusing workspace root {root}: /tmp is wiped on reboot, which caused this problem. "
                         "Choose a persistent path (or pass --allow-tmp).")

    engine = create_engine(make_url(settings.DATABASE_URL).set(database=args.database))
    repaired = skipped = healthy = 0
    with Session(engine) as db:
        documents = db.scalars(
            select(Document).where(Document.content_identity_group_id.is_not(None)).order_by(Document.created_at)
        ).all()
        print(f"{'APPLY' if args.apply else 'DRY RUN'} against database '{args.database}', workspace root {root}")
        for document in documents:
            if os.path.exists(document.source):
                healthy += 1
                continue
            group = db.get(ContentIdentityGroup, document.content_identity_group_id)
            original, payload = find_verified_original(db, group)
            if original is None:
                skipped += 1
                print(f"  SKIP  document {document.id[:8]} group {group.id}: {payload}")
                continue
            destination = workspace_content_path(root, group.id, original.suffix)
            print(f"  FIX   document {document.id[:8]} group {group.id}: verified original -> {destination}")
            if args.apply:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
                document.source = str(destination)
            repaired += 1
        if args.apply:
            db.commit()

    print(f"healthy: {healthy} | {'repaired' if args.apply else 'would repair'}: {repaired} | skipped: {skipped}")
    if not args.apply and repaired:
        print("Re-run with --apply to write the copies and update the database.")


if __name__ == "__main__":
    main()

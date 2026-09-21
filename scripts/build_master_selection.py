#!/usr/bin/env python3
"""Build the ingestion SELECTION for the cleaned master copy (see
docs/designs/master-copy-document-ingestion.md). Reads ONLY two local files -
the keep list (path, size) and the recorded checksums (SQLite) - and writes a
selection JSON plus a printed summary. It never touches the drive, the
database, or any document.

The selection JSON contains real paths, so it must be written to a gitignored
location (for example under knowledge/). No real path is hardcoded here.

Nothing is invented: exclusion rules are listed in the output, the large-file
threshold and the pilot size are explicit arguments (provisional, to be
calibrated on the pilot), and batch envelope limits are NOT chosen here.

Usage:
    python scripts/build_master_selection.py --keep-csv keep_files.csv --hashes all_hashes.sqlite \\
        --root /path/to/master/root --out selection.json [--exclude-folder NAME ...] [--large-bytes N] [--pilot-per-type K]
"""

import argparse
import collections
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.classification.deterministic_selector import CandidateObservation, classify  # noqa: E402
from app.classification.policy_evaluator import text_document_batch_policy  # noqa: E402

TEXT_DOCUMENT = {".md", ".txt", ".html", ".htm", ".pdf", ".docx", ".doc", ".rtf", ".pptx", ".xlsx", ".mbox", ".epub"}
STRUCTURED = {".json", ".csv", ".xml", ".ipynb"}
VENDOR = re.compile(r"/(node_modules|site-packages|\.venv|venv|\.git|__pycache__|\.cache|Cache|Code Cache|\.stversions|\.stfolder|dist-info|egg-info)/", re.I)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-csv", required=True, type=Path)
    ap.add_argument("--hashes", required=True, type=Path)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--large-bytes", type=int, default=50_000_000, help="provisional; files above this go to a later, separate class")
    ap.add_argument("--exclude-folder", action="append", default=[], help="top-level folder name to leave out (repeatable)")
    ap.add_argument("--exclude-name-contains", action="append", default=[],
                    help="leave out files whose name contains this text, case-insensitive (repeatable)")
    ap.add_argument("--heavy-text-bytes", type=int, default=1_000_000,
                    help="provisional (measured on the first pilot): txt/md/json/csv above this go to a separate 'chunk_heavy' class")
    ap.add_argument("--no-policy-check", action="store_true",
                    help="keep files the batch policy would not admit (default: only admitted files are selected)")
    ap.add_argument("--pilot-per-type", type=int, default=20, help="provisional; files per type in the proposed pilot")
    a = ap.parse_args()
    root = a.root.rstrip("/") + "/"
    policy = text_document_batch_policy()

    db = sqlite3.connect(f"file:{a.hashes}?mode=ro", uri=True)
    sha_of = {p: s for p, s in db.execute("select path, sha256 from all_hashes where sha256 is not null")}
    rows = [(r["file"], int(r["bytes"])) for r in csv.DictReader(open(a.keep_csv))]

    files, excluded = [], collections.Counter()
    for path, size in rows:
        ext = os.path.splitext(path.lower())[1]
        if ext in TEXT_DOCUMENT:
            workload = "text_document"
        elif ext in STRUCTURED:
            workload = "structured_data"
        else:
            continue                                   # not a document type (media, programs, code: other steps)
        if not path.startswith(root):
            excluded["outside the master root"] += 1
            continue
        if path[len(root):].split("/")[0] in a.exclude_folder:
            excluded["top-level folder excluded by the user"] += 1
            continue
        name = os.path.basename(path)
        if name.startswith("~$"):
            excluded["Office lock file (~$...)"] += 1
            continue
        if any(x.lower() in name.lower() for x in a.exclude_name_contains):
            excluded["name excluded by the user"] += 1
            continue
        if VENDOR.search(path):
            excluded["vendor / cache folder"] += 1
            continue
        if path not in sha_of:
            excluded["no recorded checksum"] += 1
            continue
        if not a.no_policy_check:
            c = classify(CandidateObservation(path, None, size))
            if not policy.matches(c):
                reason = ("type not admitted by the batch policy" if c.workload_category.value not in ("text_document", "structured_data")
                          else "risk tier too high for the batch policy (size)")
                excluded[f"{reason} ({ext})" if reason.startswith("type") else reason] += 1
                continue
        rel = path[len(root):]
        files.append({"path": path, "size": size, "sha256": sha_of[path], "ext": ext, "workload": workload,
                      "top": rel.split("/")[0], "class": ("large" if size > a.large_bytes else
                                "chunk_heavy" if ext in {".txt", ".md", ".json", ".csv"} and size > a.heavy_text_bytes else "normal")})
    files.sort(key=lambda f: (f["top"], f["path"]))

    groups = collections.defaultdict(lambda: {"files": 0, "bytes": 0})
    for f in files:
        g = groups[(f["top"], f["class"])]
        g["files"] += 1
        g["bytes"] += f["size"]

    # deterministic pilot: per type, round-robin across top-level folders, ordered by checksum (no randomness)
    pilot = []
    for ext in sorted({f["ext"] for f in files}):
        by_top = collections.defaultdict(list)
        for f in files:
            if f["ext"] == ext and f["class"] == "normal":
                by_top[f["top"]].append(f)
        for lst in by_top.values():
            lst.sort(key=lambda f: f["sha256"])
        picked, tops = [], sorted(by_top)
        i = 0
        while len(picked) < a.pilot_per_type and any(by_top[t] for t in tops):
            t = tops[i % len(tops)]
            if by_top[t]:
                picked.append(by_top[t].pop(0)["path"])
            i += 1
        pilot.extend(picked)

    selection = {
        "root": a.root,
        "source": {"keep_csv_sha256": hashlib.sha256(a.keep_csv.read_bytes()).hexdigest(), "hashes_db": a.hashes.name},
        "rules": {"document_types": sorted(TEXT_DOCUMENT | STRUCTURED), "excluded_folders_regex": VENDOR.pattern,
                  "large_bytes": a.large_bytes, "pilot_per_type": a.pilot_per_type,
                  "excluded_top_folders": a.exclude_folder, "excluded_name_contains": a.exclude_name_contains,
                  "heavy_text_bytes": a.heavy_text_bytes,
                  "policy_version": None if a.no_policy_check else policy.selection_policy_version},
        "excluded_counts": dict(excluded),
        "groups": [{"top": k[0], "class": k[1], **v} for k, v in sorted(groups.items())],
        "pilot": pilot,
        "files": files,
    }
    a.out.write_text(json.dumps(selection, ensure_ascii=False))

    n = len(files); b = sum(f["size"] for f in files)
    normal = [f for f in files if f["class"] == "normal"]
    print(f"candidate documents: {n:,} = {b/1e9:,.2f} GB   (normal {len(normal):,} = {sum(f['size'] for f in normal)/1e9:.2f} GB;"
          f" large >{a.large_bytes/1e6:.0f} MB: {sum(1 for f in files if f['class']=='large')};"
          f" chunk_heavy text >{a.heavy_text_bytes/1e6:.1f} MB: {sum(1 for f in files if f['class']=='chunk_heavy')})")
    print("excluded by rule:", dict(excluded))
    by_type = collections.Counter(f["ext"] for f in files)
    print("by type:", by_type.most_common(12))
    print("by workload:", dict(collections.Counter(f["workload"] for f in files)))
    tops = collections.Counter(); tn = collections.Counter()
    for f in files:
        tops[f["top"]] += f["size"]; tn[f["top"]] += 1
    print("top folders (files, MB):", [(t[:32], tn[t], round(v / 1e6)) for t, v in tops.most_common(10)])
    print(f"folders: {len(tops)} | groups (folder x class): {len(groups)}")
    print(f"proposed pilot: {len(pilot)} files ({sum(1 for p in pilot)} paths), types: {dict(collections.Counter(os.path.splitext(p.lower())[1] for p in pilot))}")
    print("wrote:", a.out)


if __name__ == "__main__":
    main()

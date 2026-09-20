#!/usr/bin/env python3
"""Plan-only: list leftover identical COPIES of archives whose content has already
been verified (extracted or covered) by extract_unique_and_retire_archives.py.

An archive file qualifies when: it is an exact duplicate (per the SHA-256
duplicate report) of an archive that was processed and whose processed copy is
now gone, it still exists, and it is unchanged since the metadata scan. Copies of
archives that were KEPT (still on disk) are never listed. Reads local files only;
deletes nothing.

Usage:
    python scripts/build_archive_copies_plan.py --scan-db scan.sqlite --member-hashes h.sqlite \\
        --dup-report duplicate_analysis.json --out copies.csv
"""

import argparse
import csv
import json
import os
import sqlite3
from pathlib import Path

MTIME_SLACK = 2.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan-db", required=True, type=Path)
    ap.add_argument("--member-hashes", required=True, type=Path)
    ap.add_argument("--dup-report", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    scan = sqlite3.connect(f"file:{a.scan_db}?mode=ro", uri=True)
    mem = sqlite3.connect(f"file:{a.member_hashes}?mode=ro", uri=True)
    processed = {r[0] for r in mem.execute("select archive_path from archive_done")}
    errored = {r[0] for r in mem.execute("select distinct archive_path from member_hashes where error is not null")}
    groups = json.load(open(a.dup_report))["exact_duplicate_groups"]
    rows = []
    for g in groups:
        reps = [p for p in g["paths"] if p in processed]
        if not reps:
            continue
        # every processed member of the group must be gone (done) and none problem-flagged
        if any(os.path.exists(r) for r in reps) or any(r in errored for r in reps):
            continue
        for p in g["paths"]:
            if p in processed or not os.path.isfile(p) or os.path.islink(p):
                continue
            row = scan.execute("select size, mtime from files where path=?", (p,)).fetchone()
            st = os.stat(p)
            if not row or st.st_size != row[0] or abs(st.st_mtime - row[1]) > MTIME_SLACK:
                continue
            rows.append((p, st.st_size, reps[0], int("coursera" in p.lower())))
    rows.sort(key=lambda r: -r[1])
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "bytes", "verified_original_that_was_processed", "in_coursera_folder"])
        w.writerows(rows)
    print(f"identical archive copies that can be removed: {len(rows)} = {sum(r[1] for r in rows)/1e9:,.1f} GB")
    print(f"  of which in a Coursera folder: {sum(r[3] for r in rows)} = {sum(r[1] for r in rows if r[3])/1e9:,.1f} GB")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Checksum the remaining loose files that could still match something inside
an archive: files with no checksum yet whose SIZE equals the size of a hashed
archive member (any name), plus duplicate-report group members whose size
differs from the report or that changed after it. Adds rows to the same output
SQLite that hash_loose_candidates.py writes. Strictly read-only on the files.

Usage:
    python scripts/hash_gap_files.py --scan-db scan.sqlite --member-hashes-db m.sqlite \\
        --dup-report duplicate_analysis.json --out loose_hashes.sqlite [--workers 4]
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hash_loose_candidates import SCHEMA, hash_file  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan-db", required=True, type=Path)
    ap.add_argument("--member-hashes-db", required=True, type=Path)
    ap.add_argument("--dup-report", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    scan = sqlite3.connect(f"file:{a.scan_db}?mode=ro", uri=True)
    mem = sqlite3.connect(f"file:{a.member_hashes_db}?mode=ro", uri=True)
    out = sqlite3.connect(a.out)
    out.executescript(SCHEMA)
    report = json.load(open(a.dup_report))
    analyzed_at = datetime.datetime.fromisoformat(report["analyzed_at"]).timestamp()
    ingroup = {p: g["size_bytes"] for g in report["exact_duplicate_groups"] for p in g["paths"]}
    done = {r[0] for r in out.execute("select path from loose_hashes")}
    sizes = {r[0] for r in mem.execute("select distinct size from member_hashes where size>0 and sha256 is not null")}

    todo = []
    for path, size, mtime in scan.execute("select path,size,mtime from files where size>0"):
        if path in done:
            continue
        if path in ingroup:
            if ingroup[path] != size or mtime > analyzed_at:
                todo.append((path, size, mtime))
        elif size in sizes or mtime > analyzed_at:
            todo.append((path, size, mtime))
    print(f"files to read: {len(todo):,} = {sum(t[1] for t in todo)/1e9:,.2f} GB", flush=True)
    if a.dry_run:
        return

    def work(t):
        path, size, mtime = t
        try:
            b = os.stat(path)
            sha, crc, n = hash_file(path)
            e = os.stat(path)
            changed = int(b.st_size != e.st_size or b.st_mtime != e.st_mtime or n != e.st_size)
            return t, (sha, crc, changed, None)
        except OSError as exc:
            return t, (None, None, None, f"{type(exc).__name__}: {exc}")

    started = time.time()
    errors = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = [pool.submit(work, t) for t in todo]
        for n, fut in enumerate(as_completed(futs), 1):
            (path, size, mtime), (sha, crc, changed, err) = fut.result()
            out.execute("INSERT OR REPLACE INTO loose_hashes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (path, size, mtime, sha, crc, None, 1, None, None, changed, err))
            errors += 1 if err else 0
            if n % 5000 == 0 or n == len(futs):
                out.commit()
                print(f"{time.time()-started:6.0f}s  {n:,}/{len(futs):,}  errors={errors}", flush=True)
    out.commit()
    print(f"DONE in {time.time()-started:,.0f}s, errors={errors}")


if __name__ == "__main__":
    main()

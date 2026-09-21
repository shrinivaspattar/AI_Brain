#!/usr/bin/env python3
"""Read-only: re-hash every file on a keep list and compare with the SHA-256
recorded earlier (all_hashes.sqlite). Reports missing, changed and OK counts.
Nothing is written except the printed report and an optional CSV of problems.

Usage:
    python scripts/verify_kept_files.py --keep-csv keep_files.csv --hashes all_hashes.sqlite [--workers 4]
"""

import argparse
import csv
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hash_loose_candidates import hash_file  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-csv", required=True, type=Path)
    ap.add_argument("--hashes", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--problems-csv", type=Path)
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{a.hashes}?mode=ro", uri=True)
    expected = {p: s for p, s in db.execute("select path, sha256 from all_hashes where sha256 is not null")}
    files = [r["file"] for r in csv.DictReader(open(a.keep_csv))]
    print(f"files to verify: {len(files):,}", flush=True)
    ok = missing = changed = 0
    problems = []
    started = time.time()

    def work(p):
        try:
            sha, _crc, _n = hash_file(p)
        except OSError as exc:
            return p, "missing", str(exc)[:80]
        return p, ("ok" if sha == expected.get(p) else "changed"), ""

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = [pool.submit(work, p) for p in files]
        for n, fut in enumerate(as_completed(futs), 1):
            p, status, why = fut.result()
            if status == "ok":
                ok += 1
            else:
                missing += status == "missing"
                changed += status == "changed"
                problems.append((p, status, why))
            if n % 5000 == 0:
                print(f"{time.time()-started:6.0f}s  {n:,}/{len(files):,}  ok={ok} missing={missing} changed={changed}", flush=True)
    if a.problems_csv:
        with open(a.problems_csv, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["file", "status", "detail"]); w.writerows(problems)
    print(f"DONE in {time.time()-started:,.0f}s: ok={ok:,} missing={missing} changed={changed}")


if __name__ == "__main__":
    main()

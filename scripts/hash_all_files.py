#!/usr/bin/env python3
"""Read-only: SHA-256 (and CRC-32) of EVERY non-empty file listed in a metadata
scan, into a SQLite file. Files are opened for reading only; nothing under the
scanned tree is written. Resumable: paths already in the output are skipped, so
an interrupted run can simply be started again. Each file's size and modified
time are compared before and after reading (changed_during_read).

Usage:
    python scripts/hash_all_files.py --scan-db scan.sqlite --out all_hashes.sqlite [--workers 4]
"""

import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hash_loose_candidates import hash_file  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS all_hashes (
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, sha256 TEXT, crc32 INTEGER,
    changed_during_read INTEGER, error TEXT
);
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan-db", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    scan = sqlite3.connect(f"file:{a.scan_db}?mode=ro", uri=True)
    out = sqlite3.connect(a.out)
    out.executescript(SCHEMA)
    done = {r[0] for r in out.execute("select path from all_hashes")}
    todo = [(p, s, m) for p, s, m in scan.execute("select path, size, mtime from files where size>0 order by path") if p not in done]
    total = sum(t[1] for t in todo)
    print(f"already done: {len(done):,} | to read: {len(todo):,} files = {total/1e9:,.1f} GB", flush=True)

    def work(t):
        path, size, mtime = t
        try:
            b = os.stat(path)
            sha, crc, n = hash_file(path)
            e = os.stat(path)
            changed = int(b.st_size != e.st_size or b.st_mtime != e.st_mtime or n != e.st_size)
            return t, (sha, crc, changed, None)
        except OSError as exc:
            return t, (None, None, None, f"{type(exc).__name__}: {exc}"[:200])

    started = time.time()
    read = errors = changed = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        # submit in bounded windows so a huge list does not sit in memory as futures
        it = iter(todo)
        window = set()
        n = 0
        def submit_more():
            while len(window) < a.workers * 8:
                t = next(it, None)
                if t is None:
                    return
                window.add(pool.submit(work, t))
        submit_more()
        while window:
            for fut in as_completed(list(window)):
                window.discard(fut)
                (path, size, mtime), (sha, crc, ch, err) = fut.result()
                out.execute("INSERT OR REPLACE INTO all_hashes VALUES (?,?,?,?,?,?,?)",
                            (path, size, mtime, sha, crc, ch, err))
                n += 1
                if err:
                    errors += 1
                else:
                    read += size
                    changed += 1 if ch else 0
                if n % 2000 == 0:
                    out.commit()
                    el = time.time() - started
                    rate = read / max(el, 1e-9) / 1e6
                    eta = (total - read) / max(rate * 1e6, 1) / 60
                    print(f"{el:7.0f}s  {n:,}/{len(todo):,}  {read/1e9:7.1f}/{total/1e9:,.0f} GB  {rate:5.0f} MB/s  "
                          f"errors={errors} changed={changed}  ~{eta:,.0f} min left", flush=True)
                submit_more()
                break
    out.commit()
    print(f"DONE in {time.time()-started:,.0f}s: read {read/1e9:,.1f} GB, errors={errors}, changed-while-reading={changed}")


if __name__ == "__main__":
    main()

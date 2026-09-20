#!/usr/bin/env python3
"""Read-only: checksum every file in one or more newly extracted folders and say,
per file, whether the same content already exists elsewhere on the drive right
now. Nothing is written except the output CSV files; nothing is deleted.

A file is a DUPLICATE when its SHA-256 equals one recorded in the manifest for a
loose file that still exists (same size and modified time as when scanned) outside
the given folders, or equals a file already extracted and verified by
extract_unique_and_retire_archives.py. Files whose content appears nowhere else
are UNIQUE; content that appears several times only inside the given folders is
REPEAT (keep one).

Usage:
    python scripts/compare_new_folders.py --manifest m.sqlite --scan-db scan.sqlite \\
        --verified-csv _verified_files.csv --out-dir DIR --folder F1 --folder F2
"""

import argparse
import collections
import csv
import hashlib
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CHUNK = 4 * 1024 * 1024
EMPTY = hashlib.sha256(b"").hexdigest()


def hash_path(p):
    sha = hashlib.sha256()
    with open(p, "rb", buffering=0) as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            sha.update(block)
    return sha.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--scan-db", required=True, type=Path)
    ap.add_argument("--verified-csv", type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--folder", action="append", required=True)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    folders = [os.path.abspath(f) for f in a.folder]
    a.out_dir.mkdir(parents=True, exist_ok=True)

    files = []
    for f in folders:
        for root, _d, names in os.walk(f):
            for n in names:
                p = os.path.join(root, n)
                if os.path.islink(p):
                    continue
                files.append((p, os.path.getsize(p)))
    print(f"files to read: {len(files):,} = {sum(s for _p, s in files)/1e9:,.1f} GB", flush=True)

    hashes = {}
    started = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(hash_path, p): (p, s) for p, s in files if s}
        for n, fut in enumerate(as_completed(futs), 1):
            p, s = futs[fut]
            try:
                hashes[p] = fut.result()
            except OSError as exc:
                hashes[p] = "ERR:" + str(exc)[:60]
            if n % 2000 == 0:
                print(f"{time.time()-started:6.0f}s  {n:,}/{len(futs):,}", flush=True)

    man = sqlite3.connect(f"file:{a.manifest}?mode=ro", uri=True)
    scan = sqlite3.connect(f"file:{a.scan_db}?mode=ro", uri=True)
    want = set(hashes.values())
    loose = collections.defaultdict(list)
    for sha, loc in man.execute("select sha, location from locations where kind='loose'"):
        if sha in want:
            loose[sha].append(loc)
    verified = {}
    if a.verified_csv and a.verified_csv.exists():
        for sha, _size, path in csv.reader(open(a.verified_csv)):
            if os.path.isfile(path):
                verified[sha] = path

    def inside(p):
        return any(p.startswith(f + os.sep) for f in folders)

    def live_copy(sha):
        for loc in loose.get(sha, ()):
            if inside(loc):
                continue
            row = scan.execute("select size, mtime from files where path=?", (loc,)).fetchone()
            try:
                st = os.stat(loc)
            except OSError:
                continue
            if row and st.st_size == row[0] and abs(st.st_mtime - row[1]) <= 2.0:
                return loc
        return verified.get(sha)

    seen_in_new = {}
    rows = []
    for p, s in sorted(files):
        if s == 0:
            rows.append((p, 0, EMPTY, "duplicate", "(empty file)"))
            continue
        sha = hashes[p]
        if sha.startswith("ERR:"):
            rows.append((p, s, "", "error", sha))
            continue
        other = live_copy(sha)
        if other:
            rows.append((p, s, sha, "duplicate", other))
        elif sha in seen_in_new:
            rows.append((p, s, sha, "repeat", seen_in_new[sha]))
        else:
            seen_in_new[sha] = p
            rows.append((p, s, sha, "unique", ""))
    with open(a.out_dir / "new_folders_files.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "bytes", "sha256", "status", "same_content_at"])
        w.writerows(rows)

    # folders in which EVERY file is a duplicate/repeat -> removable as a whole
    bad = collections.Counter()
    total = collections.Counter()
    size = collections.Counter()
    for p, s, _sha, st, _o in rows:
        d = os.path.dirname(p)
        while any(d == f or d.startswith(f + os.sep) for f in folders):
            total[d] += 1
            size[d] += s
            if st in ("unique", "error"):
                bad[d] += 1
            if d in folders:
                break
            d = os.path.dirname(d)
    ok = [d for d in total if bad[d] == 0]
    okset = set(ok)
    maximal = [d for d in ok if os.path.dirname(d) not in okset]
    with open(a.out_dir / "new_folders_removable.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["folder", "files", "bytes"])
        for d in sorted(maximal, key=lambda d: -size[d]):
            w.writerow([d, total[d], size[d]])
    cnt = collections.Counter(r[3] for r in rows)
    by = collections.Counter()
    for r in rows:
        by[r[3]] += r[1]
    print("RESULT:")
    for k in ("duplicate", "repeat", "unique", "error"):
        print(f"  {k:10s} {cnt[k]:>8,} files  {by[k]/1e9:8.2f} GB")
    print(f"  folders that are 100% duplicates (removable whole): {len(maximal):,} = {sum(size[d] for d in maximal)/1e9:,.1f} GB")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Checksum (SHA-256 + CRC-32) the loose files that could be copies of
something inside an archive, so archive members can later be matched by
content.

Candidates = loose files whose size AND lower-cased file name equal a distinct
archive member's (from scan_archive_contents.py), plus any file modified after
the duplicate report was made (its checksum, if any, is stale or missing).
When a duplicate report is given, files it says are identical are read ONCE
(one representative per identical group): the other copies share its content.

Strictly read-only: files are opened for reading only, in 4 MiB chunks, and
nothing is written anywhere except the output SQLite file. Resumable: paths
already in the output are skipped, so an interrupted run can be repeated.
Each file's size and modified time are re-checked after reading, and a
representative's SHA-256 is compared with the duplicate report's, which also
tells how stale that report is.

No real path is hardcoded; all inputs and outputs are arguments.

Usage:
    python scripts/hash_loose_candidates.py --scan-db scan.sqlite \\
        --archives-db archives.sqlite --dup-report duplicate_analysis.json \\
        --out loose_hashes.sqlite [--dry-run] [--workers 4]
"""

import argparse
import collections
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CHUNK = 4 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS loose_hashes (
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, sha256 TEXT, crc32 INTEGER,
    group_id INTEGER, group_copies INTEGER, d1_sha256 TEXT, matches_d1 INTEGER,
    changed_during_read INTEGER, error TEXT
);
"""


def hash_file(path: str) -> tuple[str, int, int]:
    """Returns (sha256 hex, crc32, bytes read). Opens read-only."""
    sha = hashlib.sha256()
    crc = 0
    total = 0
    with open(path, "rb", buffering=0) as fh:
        try:
            os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_SEQUENTIAL)
        except (AttributeError, OSError):
            pass
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            sha.update(block)
            crc = zlib.crc32(block, crc)
            total += len(block)
        try:
            os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
    return sha.hexdigest(), crc & 0xFFFFFFFF, total


def build_candidates(scan_db: Path, archives_db: Path, dup_report: Path | None):
    scan = sqlite3.connect(f"file:{scan_db}?mode=ro", uri=True)
    arch = sqlite3.connect(f"file:{archives_db}?mode=ro", uri=True)

    wanted = set()  # (size, lower basename) of distinct archive members
    for name, size in arch.execute("select name, size from members where is_dir=0 and size>0"):
        wanted.add((size, os.path.basename(name.replace("\\", "/")).lower()))

    group_of: dict[str, int] = {}
    group_paths: dict[int, list[str]] = {}
    group_hash: dict[int, str] = {}
    analyzed_at = None
    if dup_report:
        report = json.load(open(dup_report))
        analyzed_at = datetime.datetime.fromisoformat(report["analyzed_at"]).timestamp()
        for i, g in enumerate(report["exact_duplicate_groups"]):
            group_paths[i] = g["paths"]
            group_hash[i] = g["content_hash"]
            for p in g["paths"]:
                group_of[p] = i

    matched: dict[str, tuple[int, float]] = {}
    for path, size, mtime in scan.execute("select path, size, mtime from files where size>0"):
        base = os.path.basename(path).lower()
        if (size, base) in wanted or (analyzed_at is not None and mtime > analyzed_at):
            matched[path] = (size, mtime)

    chosen: dict[object, tuple[str, int, float]] = {}
    for path, (size, mtime) in matched.items():
        g = group_of.get(path)
        key = ("g", g) if g is not None else ("p", path)
        current = chosen.get(key)
        if current is None or (len(path), path) < (len(current[0]), current[0]):
            chosen[key] = (path, size, mtime)
    todo = []
    for (kind, ident), (path, size, mtime) in chosen.items():
        g = ident if kind == "g" else None
        todo.append({
            "path": path, "size": size, "mtime": mtime, "group_id": g,
            "group_copies": len(group_paths[g]) if g is not None else 1,
            "d1_sha256": group_hash.get(g) if g is not None else None,
        })
    todo.sort(key=lambda r: r["path"])
    return todo, len(matched)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scan-db", required=True, type=Path)
    parser.add_argument("--archives-db", required=True, type=Path)
    parser.add_argument("--dup-report", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true", help="count what would be read; read nothing")
    args = parser.parse_args()

    todo, matched_files = build_candidates(args.scan_db, args.archives_db, args.dup_report)
    total_bytes = sum(r["size"] for r in todo)
    print(f"matching loose files: {matched_files:,} | files to read (one per identical group): "
          f"{len(todo):,} = {total_bytes/1e9:,.1f} GB", flush=True)
    if args.dry_run:
        big = sorted(todo, key=lambda r: -r["size"])[:3]
        print("largest three:", [f"{r['size']/1e9:.2f} GB" for r in big])
        return

    out = sqlite3.connect(args.out)
    out.executescript(SCHEMA)
    done = {row[0] for row in out.execute("select path from loose_hashes")}
    todo = [r for r in todo if r["path"] not in done]
    print(f"already done: {len(done):,} | remaining: {len(todo):,}", flush=True)

    def work(rec):
        path = rec["path"]
        try:
            before = os.stat(path)
            sha, crc, nread = hash_file(path)
            after = os.stat(path)
            changed = int(before.st_size != after.st_size or before.st_mtime != after.st_mtime or nread != after.st_size)
            matches = None if rec["d1_sha256"] is None else int(sha == rec["d1_sha256"])
            return rec, (sha, crc, changed, matches, None)
        except OSError as exc:
            return rec, (None, None, None, None, f"{type(exc).__name__}: {exc}")

    started = time.time()
    read_bytes = errors = stale = changed_count = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, r) for r in todo]
        for n, fut in enumerate(as_completed(futures), 1):
            rec, (sha, crc, changed, matches, err) = fut.result()
            out.execute("INSERT OR REPLACE INTO loose_hashes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (rec["path"], rec["size"], rec["mtime"], sha, crc, rec["group_id"], rec["group_copies"],
                         rec["d1_sha256"], matches, changed, err))
            if err:
                errors += 1
            else:
                read_bytes += rec["size"]
                stale += 1 if matches == 0 else 0
                changed_count += 1 if changed else 0
            if n % 500 == 0 or n == len(futures):
                out.commit()
                rate = read_bytes / max(time.time() - started, 1e-9) / 1e6
                print(f"{time.time()-started:6.0f}s  {n:,}/{len(futures):,}  {read_bytes/1e9:6.1f} GB  "
                      f"{rate:6.0f} MB/s  errors={errors} d1_mismatch={stale} changed={changed_count}", flush=True)
    out.commit()
    print(f"DONE in {time.time()-started:,.0f}s: read {read_bytes/1e9:,.1f} GB, errors={errors}, "
          f"differs-from-report={stale}, changed-while-reading={changed_count}")


if __name__ == "__main__":
    main()

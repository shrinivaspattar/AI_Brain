#!/usr/bin/env python3
"""Read the TABLE OF CONTENTS of archives found by scan_tree_metadata.py, into
a SQLite file. Nothing is extracted and nothing under the scanned tree is
written: zip and 7z archives are opened read-only and only their headers /
central directory are read (member name, size, CRC-32); .rar is listed via the
`7z l -slt` command, which also only reads headers.

Not listed (recorded with a status instead): tar-family archives and plain
.gz/.bz2/.xz (listing them needs a full sequential read/decompress of the
whole file) and disk images (.iso/.img).

If a duplicate report (duplicate_analysis.json) is given, identical copies of
the same archive are listed ONCE (via one existing representative path), and
the number of copies is recorded.

No real path is hardcoded; inputs and outputs are arguments.

Usage:
    python scripts/scan_archive_contents.py --scan-db scan.sqlite \\
        --dup-report duplicate_analysis.json --out archives.sqlite
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

LISTABLE = {".zip": "zip", ".7z": "7z", ".rar": "rar"}
NOT_LISTED = {
    ".tar": "stream", ".tgz": "stream", ".gz": "stream", ".bz2": "stream", ".xz": "stream",
    ".iso": "image", ".img": "image",
}
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".iso", ".img"}

SCHEMA = """
CREATE TABLE archives (
    path TEXT PRIMARY KEY, size INTEGER, kind TEXT, status TEXT, error TEXT,
    copies INTEGER, member_count INTEGER, dir_count INTEGER, uncompressed_bytes INTEGER
);
CREATE TABLE members (
    archive_path TEXT, name TEXT, size INTEGER, crc32 INTEGER, is_dir INTEGER, nested_archive INTEGER
);
"""


def safe(text) -> str:
    return str(text).encode("utf-8", errors="replace").decode("utf-8")


def is_archive_name(name: str) -> bool:
    return os.path.splitext(name.lower())[1] in ARCHIVE_EXTS


def list_zip(path: str):
    with zipfile.ZipFile(path, "r") as zf:
        for info in zf.infolist():
            yield info.filename, info.file_size, info.CRC, info.is_dir()


def list_7z(path: str):
    import py7zr

    with py7zr.SevenZipFile(path, mode="r") as archive:
        if archive.needs_password():
            raise PermissionError("archive is encrypted (password required)")
        for f in archive.list():
            yield f.filename, f.uncompressed or 0, f.crc32, bool(f.is_directory)


def list_rar(path: str):
    out = subprocess.run(["7z", "l", "-slt", "-ba", "--", path], capture_output=True, text=True, timeout=600)
    if out.returncode not in (0, 1):
        raise RuntimeError((out.stderr or out.stdout).strip()[:200] or f"7z exit {out.returncode}")
    record: dict[str, str] = {}
    for line in out.stdout.splitlines() + [""]:
        if not line.strip():
            if "Path" in record:
                crc = record.get("CRC")
                yield (record["Path"], int(record.get("Size") or 0),
                       int(crc, 16) if crc else None, record.get("Folder") == "+")
            record = {}
        elif " = " in line:
            key, value = line.split(" = ", 1)
            record[key.strip()] = value.strip()


LISTERS = {"zip": list_zip, "7z": list_7z, "rar": list_rar}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scan-db", required=True, type=Path)
    parser.add_argument("--dup-report", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"{args.out} already exists - refusing to overwrite")

    scan = sqlite3.connect(f"file:{args.scan_db}?mode=ro", uri=True)
    archives = {p: s for p, s, in scan.execute(
        "select path, size from files where ext in (%s)" % ",".join("?" * len(ARCHIVE_EXTS)), tuple(ARCHIVE_EXTS))}
    print(f"archive-type files in the scan: {len(archives):,}", flush=True)

    copies_of: dict[str, int] = {}
    if args.dup_report:
        groups = json.load(open(args.dup_report))["exact_duplicate_groups"]
        by_path = {}
        for g in groups:
            for p in g["paths"]:
                by_path[p] = g["paths"]
        chosen: dict[str, str] = {}
        for p in archives:
            group = by_path.get(p)
            key = group[0] if group else p
            rep = min((q for q in (group or [p]) if q in archives), key=lambda q: (len(q), q))
            chosen[key if group else p] = rep
            copies_of[rep] = len(group) if group else 1
        todo = sorted(set(chosen.values()))
    else:
        todo = sorted(archives)
        copies_of = {p: 1 for p in todo}
    print(f"distinct archives to read: {len(todo):,}", flush=True)

    out = sqlite3.connect(args.out)
    out.executescript(SCHEMA)
    started = time.time()
    ok = failed = skipped = 0
    for i, path in enumerate(todo, 1):
        ext = os.path.splitext(path.lower())[1]
        size = archives[path]
        kind = LISTABLE.get(ext) or NOT_LISTED.get(ext) or "unknown"
        if kind not in LISTERS:
            out.execute("INSERT INTO archives VALUES (?,?,?,?,?,?,?,?,?)",
                        (safe(path), size, kind, "not_listed", None, copies_of[path], None, None, None))
            skipped += 1
            continue
        rows = []
        try:
            for name, msize, crc, is_dir in LISTERS[kind](path):
                rows.append((safe(path), safe(name), msize, crc, int(is_dir), int(is_archive_name(name))))
            out.executemany("INSERT INTO members VALUES (?,?,?,?,?,?)", rows)
            dirs = sum(r[4] for r in rows)
            out.execute("INSERT INTO archives VALUES (?,?,?,?,?,?,?,?,?)",
                        (safe(path), size, kind, "ok", None, copies_of[path], len(rows), dirs,
                         sum(r[2] for r in rows if not r[4])))
            ok += 1
        except Exception as exc:  # noqa: BLE001 - record and continue
            status = "encrypted" if isinstance(exc, PermissionError) or "assword" in str(exc) else "error"
            out.execute("INSERT INTO archives VALUES (?,?,?,?,?,?,?,?,?)",
                        (safe(path), size, kind, status, safe(f"{type(exc).__name__}: {exc}")[:300],
                         copies_of[path], None, None, None))
            failed += 1
        out.commit()
        if i % 10 == 0 or i == len(todo):
            print(f"{time.time()-started:6.0f}s  {i}/{len(todo)}  listed={ok} failed={failed} not_listed={skipped}", flush=True)

    out.execute("CREATE INDEX ix_members_size_crc ON members(size, crc32)")
    out.execute("CREATE INDEX ix_members_archive ON members(archive_path)")
    out.commit()
    print(f"DONE in {time.time()-started:,.0f}s: listed={ok} failed={failed} not_listed={skipped}")


if __name__ == "__main__":
    main()

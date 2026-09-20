#!/usr/bin/env python3
"""Read-only METADATA scan of a directory tree into a SQLite file.

Records, for every file: path, directory, name, extension, size, modified time,
link count and depth. It never opens a file (no contents are read), never
follows symlinks, and writes only the SQLite output file - nothing under the
scanned root is created, changed or deleted.

Errors (unreadable directories, odd filenames) are counted and stored, not
fatal. Progress is printed every --progress files.

No real path is hardcoded here: the root and output are arguments, and the
default output location (knowledge/) is gitignored for real-drive scans.

Usage:
    python scripts/scan_tree_metadata.py --root /path/to/scan --out scan.sqlite
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE files (
    path   TEXT PRIMARY KEY,
    dir    TEXT NOT NULL,
    name   TEXT NOT NULL,
    ext    TEXT NOT NULL,
    size   INTEGER NOT NULL,
    mtime  REAL NOT NULL,
    nlink  INTEGER NOT NULL,
    depth  INTEGER NOT NULL
);
CREATE TABLE dirs (
    path  TEXT PRIMARY KEY,
    depth INTEGER NOT NULL
);
CREATE TABLE symlinks (path TEXT PRIMARY KEY, target TEXT);
CREATE TABLE scan_errors (path TEXT, error TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def safe(text: str) -> str:
    """SQLite TEXT must be valid UTF-8; replace undecodable bytes rather than fail."""
    return text.encode("utf-8", errors="replace").decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--progress", type=int, default=50_000)
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"--root {root} is not a directory")
    if args.out.exists():
        raise SystemExit(f"{args.out} already exists - refusing to overwrite")
    try:
        args.out.resolve().relative_to(root)
        raise SystemExit("--out must not be inside the scanned root (the scan is read-only)")
    except ValueError:
        pass

    db = sqlite3.connect(args.out)
    db.executescript(SCHEMA)
    root_depth = len(root.parts)
    started = time.time()
    n_files = n_dirs = n_err = n_links = 0
    total_bytes = 0
    batch: list[tuple] = []

    def flush() -> None:
        if batch:
            db.executemany("INSERT OR IGNORE INTO files VALUES (?,?,?,?,?,?,?,?)", batch)
            db.commit()
            batch.clear()

    stack = [root]
    db.execute("INSERT INTO dirs VALUES (?, ?)", (safe(str(root)), 0))
    while stack:
        current = stack.pop()
        depth = len(current.parts) - root_depth
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            n_links += 1
                            try:
                                target = os.readlink(entry.path)
                            except OSError:
                                target = None
                            db.execute("INSERT OR IGNORE INTO symlinks VALUES (?,?)", (safe(entry.path), safe(target) if target else None))
                        elif entry.is_dir(follow_symlinks=False):
                            n_dirs += 1
                            db.execute("INSERT OR IGNORE INTO dirs VALUES (?,?)", (safe(entry.path), depth + 1))
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            name = entry.name
                            ext = os.path.splitext(name)[1].lower()
                            batch.append((safe(entry.path), safe(str(current)), safe(name), safe(ext),
                                          st.st_size, st.st_mtime, st.st_nlink, depth + 1))
                            n_files += 1
                            total_bytes += st.st_size
                            if len(batch) >= 5000:
                                flush()
                            if n_files % args.progress == 0:
                                print(f"{time.time()-started:7.0f}s  files={n_files:,}  dirs={n_dirs:,}  "
                                      f"bytes={total_bytes/1e9:,.1f} GB  errors={n_err}", flush=True)
                    except OSError as exc:
                        n_err += 1
                        db.execute("INSERT INTO scan_errors VALUES (?,?)", (safe(entry.path), str(exc)))
        except OSError as exc:
            n_err += 1
            db.execute("INSERT INTO scan_errors VALUES (?,?)", (safe(str(current)), str(exc)))

    flush()
    elapsed = time.time() - started
    for key, value in (("root", str(root)), ("files", n_files), ("dirs", n_dirs), ("symlinks", n_links),
                       ("total_bytes", total_bytes), ("errors", n_err), ("elapsed_seconds", round(elapsed, 1)),
                       ("scanned_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))):
        db.execute("INSERT INTO meta VALUES (?,?)", (key, safe(str(value))))
    db.execute("CREATE INDEX ix_files_dir ON files(dir)")
    db.execute("CREATE INDEX ix_files_ext ON files(ext)")
    db.execute("CREATE INDEX ix_files_size ON files(size)")
    db.commit()
    db.close()
    print(f"DONE in {elapsed:,.0f}s: files={n_files:,} dirs={n_dirs:,} symlinks={n_links:,} "
          f"bytes={total_bytes/1e9:,.1f} GB errors={n_err}")


if __name__ == "__main__":
    main()

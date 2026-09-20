#!/usr/bin/env python3
"""SHA-256 every file INSIDE archives (zip, 7z, rar), one archive at a time,
without writing extracted files anywhere near the scanned tree.

For each distinct archive listed by scan_archive_contents.py: members are
decompressed as a stream and hashed in memory (4 MiB chunks). The hash is also
checked against the CRC-32 stored in the archive, so a bad read is detected.
Archives nested inside archives are copied to a short-lived temp file in
--temp-dir (must be on an internal disk, not the scanned drive), opened
recursively, and the temp file is deleted right after. Nested members are
recorded with a path like ``outer.zip!inner/file.txt``.

Read-only towards the archives; the only writes are the output SQLite file and
the temp files (removed after use, capped by --max-temp-gb).
Resumable: archives already finished in the output are skipped.

No real path is hardcoded; all inputs and outputs are arguments.

Usage:
    python scripts/hash_archive_members.py --archives-db archives.sqlite \\
        --out member_hashes.sqlite --temp-dir /some/internal/dir [--limit 5] [--only text]
"""

import argparse
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

CHUNK = 4 * 1024 * 1024
NESTED_EXTS = {".zip": "zip", ".7z": "7z", ".rar": "rar"}
MAX_DEPTH = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS member_hashes (
    archive_path TEXT, member_path TEXT, depth INTEGER, size INTEGER,
    sha256 TEXT, crc32 INTEGER, crc_ok INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS archive_done (
    archive_path TEXT PRIMARY KEY, status TEXT, members INTEGER, bytes INTEGER,
    seconds REAL, error TEXT
);
"""


def safe(text) -> str:
    return str(text).encode("utf-8", errors="replace").decode("utf-8")


def nested_kind(name: str):
    return NESTED_EXTS.get(os.path.splitext(name.lower())[1])


class Ctx:
    def __init__(self, temp_dir: Path, max_temp: int):
        self.temp_dir = temp_dir
        self.max_temp = max_temp
        self.temp_used = 0
        self.rows: list[tuple] = []
        self.bytes = 0

    def add(self, top, member, depth, size, sha, crc, stored_crc, error=None):
        ok = None if (crc is None or stored_crc is None) else int(crc == stored_crc)
        self.rows.append((safe(top), safe(member), depth, size, sha, crc, ok, error))
        if size:
            self.bytes += size


def stream_hash(fh, spool=None):
    sha = hashlib.sha256()
    crc = 0
    total = 0
    while True:
        block = fh.read(CHUNK)
        if not block:
            break
        sha.update(block)
        crc = zlib.crc32(block, crc)
        total += len(block)
        if spool is not None:
            spool.write(block)
    return sha.hexdigest(), crc & 0xFFFFFFFF, total


def process(kind, path, top, prefix, depth, ctx: Ctx):
    if kind == "zip":
        process_zip(path, top, prefix, depth, ctx)
    elif kind == "7z":
        try:
            process_7z(path, top, prefix, depth, ctx)
        except Exception as exc:  # noqa: BLE001
            if "nsupported" not in type(exc).__name__ + str(exc):
                raise
            ctx.rows = [r for r in ctx.rows if r[0] != safe(top) or not r[1].startswith(prefix)]
            process_rar(path, top, prefix, depth, ctx)
    else:
        process_rar(path, top, prefix, depth, ctx)


def recurse_temp(tmp_path, kind, top, member_name, depth, ctx):
    try:
        process(kind, tmp_path, top, member_name + "!", depth + 1, ctx)
    except Exception as exc:  # noqa: BLE001
        ctx.add(top, member_name + "!", depth + 1, None, None, None, None,
                f"nested archive could not be opened: {type(exc).__name__}: {exc}"[:300])
    finally:
        try:
            ctx.temp_used -= os.path.getsize(tmp_path)
            os.unlink(tmp_path)
        except OSError:
            pass


def process_zip(path, top, prefix, depth, ctx):
    with zipfile.ZipFile(path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = prefix + info.filename
            nk = nested_kind(info.filename) if depth < MAX_DEPTH else None
            tmp = None
            try:
                with zf.open(info, "r") as fh:
                    if nk and ctx.temp_used + info.file_size <= ctx.max_temp:
                        fd, tmp = tempfile.mkstemp(dir=ctx.temp_dir, suffix=os.path.splitext(info.filename)[1])
                        with os.fdopen(fd, "wb") as spool:
                            sha, crc, total = stream_hash(fh, spool)
                        ctx.temp_used += total
                    else:
                        sha, crc, total = stream_hash(fh)
                        if nk:
                            ctx.add(top, name, depth, total, sha, crc, info.CRC,
                                    "hashed but not opened: temp limit")
                            continue
                ctx.add(top, name, depth, total, sha, crc, info.CRC)
            except Exception as exc:  # noqa: BLE001
                ctx.add(top, name, depth, info.file_size, None, None, info.CRC, f"{type(exc).__name__}: {exc}"[:300])
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
                continue
            if tmp:
                recurse_temp(tmp, nk, top, name, depth, ctx)


def process_7z(path, top, prefix, depth, ctx):
    import py7zr
    from py7zr.io import Py7zIO, WriterFactory

    if_encrypted = py7zr.SevenZipFile(path, "r")
    try:
        if if_encrypted.needs_password():
            raise PermissionError("archive is encrypted (password required)")
        stored = {f.filename: f.crc32 for f in if_encrypted.list()}
    finally:
        if_encrypted.close()

    found: dict[str, dict] = {}

    class HashIO(Py7zIO):
        def __init__(self, filename):
            self.filename = filename
            self.sha = hashlib.sha256()
            self.crc = 0
            self.total = 0
            self.spool = None
            self.tmp = None
            nk = nested_kind(filename) if depth < MAX_DEPTH else None
            self.nk = nk
            if nk:
                fd, self.tmp = tempfile.mkstemp(dir=ctx.temp_dir, suffix=os.path.splitext(filename)[1])
                self.spool = os.fdopen(fd, "wb")

        def write(self, s):
            b = bytes(s)
            self.sha.update(b)
            self.crc = zlib.crc32(b, self.crc)
            self.total += len(b)
            if self.spool is not None:
                if ctx.temp_used + len(b) > ctx.max_temp:
                    self.spool.close()
                    os.unlink(self.tmp)
                    self.spool = self.tmp = None
                    self.nk = None
                    found.setdefault("__limit__", {})[self.filename] = True
                else:
                    self.spool.write(b)
                    ctx.temp_used += len(b)
            return len(b)

        def read(self, size=None):
            return b""

        def seek(self, offset, whence=0):
            return 0

        def flush(self):
            if self.spool is not None:
                self.spool.flush()

        def size(self):
            return self.total

        def finish(self):
            if self.spool is not None:
                self.spool.close()
            found[self.filename] = {"sha": self.sha.hexdigest(), "crc": self.crc & 0xFFFFFFFF,
                                    "size": self.total, "tmp": self.tmp, "nk": self.nk}

    made: list[HashIO] = []

    class Factory(WriterFactory):
        def create(self, filename):
            io = HashIO(filename)
            made.append(io)
            return io

    with py7zr.SevenZipFile(path, "r") as archive:
        archive.extract(factory=Factory())
    for io in made:
        io.finish()
    limit_hit = found.pop("__limit__", {})
    for name, rec in found.items():
        full = prefix + name
        note = "hashed but not opened: temp limit" if name in limit_hit else None
        ctx.add(top, full, depth, rec["size"], rec["sha"], rec["crc"], stored.get(name), note)
    for name, rec in found.items():
        if rec["tmp"] and rec["nk"] and os.path.exists(rec["tmp"]):
            recurse_temp(rec["tmp"], rec["nk"], top, prefix + name, depth, ctx)


def recurse_existing(path, kind, top, member_name, depth, ctx):
    """Open an already-extracted nested archive (it is deleted with its work dir)."""
    try:
        process(kind, path, top, member_name + "!", depth + 1, ctx)
    except Exception as exc:  # noqa: BLE001
        ctx.add(top, member_name + "!", depth + 1, None, None, None, None,
                f"nested archive could not be opened: {type(exc).__name__}: {exc}"[:300])


def process_rar(path, top, prefix, depth, ctx):
    """Also the fallback for 7z archives py7zr cannot decode: the `7z` command
    extracts to a temp dir (verifying its own CRCs), which is hashed and removed."""
    workdir = tempfile.mkdtemp(dir=ctx.temp_dir)
    try:
        out = subprocess.run(["7z", "x", "-y", f"-o{workdir}", "--", path], capture_output=True, text=True, timeout=3600)
        if out.returncode not in (0, 1):
            raise RuntimeError((out.stderr or out.stdout).strip()[:200] or f"7z exit {out.returncode}")
        for root, _dirs, files in os.walk(workdir):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, workdir).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    sha, crc, total = stream_hash(fh)
                ctx.add(top, prefix + rel, depth, total, sha, crc, None)
                nk = nested_kind(f) if depth < MAX_DEPTH else None
                if nk:
                    recurse_existing(full, nk, top, prefix + rel, depth, ctx)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archives-db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--temp-dir", required=True, type=Path)
    parser.add_argument("--max-temp-gb", type=float, default=15.0)
    parser.add_argument("--limit", type=int, help="process at most this many archives in this run")
    parser.add_argument("--only", help="only archives whose path contains this text")
    parser.add_argument("--order", choices=["smallest", "largest"], default="smallest")
    args = parser.parse_args()

    args.temp_dir.mkdir(parents=True, exist_ok=True)
    arch = sqlite3.connect(f"file:{args.archives_db}?mode=ro", uri=True)
    todo = arch.execute("select path, size, kind from archives where status='ok' order by size "
                        + ("asc" if args.order == "smallest" else "desc")).fetchall()
    if args.only:
        todo = [t for t in todo if args.only in t[0]]
    out = sqlite3.connect(args.out)
    out.executescript(SCHEMA)
    done = {r[0] for r in out.execute("select archive_path from archive_done")}
    todo = [t for t in todo if safe(t[0]) not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"archives to process now: {len(todo)} = {sum(t[1] for t in todo)/1e9:,.1f} GB", flush=True)

    run_start = time.time()
    total_bytes = 0
    for i, (path, size, kind) in enumerate(todo, 1):
        ctx = Ctx(args.temp_dir, int(args.max_temp_gb * 1e9))
        started = time.time()
        status, err = "done", None
        try:
            process(kind, path, path, "", 0, ctx)
        except Exception as exc:  # noqa: BLE001
            status = "encrypted" if isinstance(exc, PermissionError) or "assword" in str(exc) else "error"
            err = safe(f"{type(exc).__name__}: {exc}")[:300]
            ctx.rows = []  # do not keep a half-read archive
        secs = time.time() - started
        out.executemany("INSERT INTO member_hashes VALUES (?,?,?,?,?,?,?,?)", ctx.rows)
        out.execute("INSERT OR REPLACE INTO archive_done VALUES (?,?,?,?,?,?)",
                    (safe(path), status, len(ctx.rows), ctx.bytes, secs, err))
        out.commit()
        total_bytes += ctx.bytes
        bad = sum(1 for r in ctx.rows if r[6] == 0)
        print(f"[{i}/{len(todo)}] {status:9s} {secs:7.1f}s  members={len(ctx.rows):>6,}  "
              f"{ctx.bytes/1e9:7.2f} GB unpacked  crc-mismatch={bad}  {os.path.basename(path)[:60]}", flush=True)
    el = time.time() - run_start
    print(f"DONE in {el:,.0f}s: {total_bytes/1e9:,.1f} GB unpacked, {total_bytes/1e6/max(el,1e-9):,.0f} MB/s", flush=True)


if __name__ == "__main__":
    main()

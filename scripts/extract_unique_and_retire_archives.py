#!/usr/bin/env python3
"""Extract ONLY the files of each archive that exist nowhere else, verify them
by SHA-256, and (only if you ask) delete the archive once every file inside is
proven safe. One archive at a time. Meant to be run by a person; review the
dry run first.

Per archive (zip / 7z / rar, nested archives included):
  1. Every non-empty file inside is looked up by SHA-256 (recorded earlier by
     hash_archive_members.py). If a loose copy of that content still exists on
     disk right now (same size and modified time as when it was scanned), the
     file is "already covered" and is NOT extracted.
  2. Files with no loose copy are extracted into
     <dest-dir>/<archive name>-<6 hex>/<path inside the archive>. Each is
     written as .part, hashed while writing, and only renamed into place when
     the hash equals the recorded one. Existing files are never overwritten.
  3. With --delete-verified, the archive file is deleted only when: it has no
     unreadable/encrypted members, it is unchanged since the scan, every needed
     file was extracted and verified, and every "already covered" copy is
     re-checked just before deleting. Otherwise it is kept and the reason is
     printed.

Default (no flags) is a DRY RUN: nothing is written or deleted.
  --extract           really extract (writes only under --dest-dir and --temp-dir)
  --delete-verified   also delete verified archives (needs --extract and --root)

Usage:
    python scripts/extract_unique_and_retire_archives.py --manifest m.sqlite \\
        --member-hashes h.sqlite --scan-db scan.sqlite --dest-dir DIR --temp-dir DIR \\
        --root /the/scanned/root [--only text] [--limit N] [--extract] [--delete-verified]
"""

import argparse
import collections
import csv
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

CHUNK = 4 * 1024 * 1024
NESTED = {".zip": "zip", ".7z": "7z", ".rar": "rar"}
LEVEL_SPLIT = re.compile(r"(?<=\.zip!)|(?<=\.7z!)|(?<=\.rar!)", re.I)
MTIME_SLACK = 2.0


def nested_kind(name):
    return NESTED.get(os.path.splitext(name.lower())[1])


MAX_NAME_UNITS = 240   # exFAT/NTFS allow 255 UTF-16 units per name; keep a margin


def clean_part(part: str) -> str:
    if part in ("", ".", ".."):
        raise ValueError("unsafe path component")
    if len(part.encode("utf-16-le")) // 2 > MAX_NAME_UNITS:
        stem, ext = os.path.splitext(part)
        ext = ext if len(ext) <= 16 else ""
        tag = "~" + hashlib.sha1(part.encode("utf-8", "replace")).hexdigest()[:8]
        room = MAX_NAME_UNITS - len(ext) - len(tag)
        stem = stem[:room]
        while len((stem + tag + ext).encode("utf-16-le")) // 2 > MAX_NAME_UNITS:
            stem = stem[:-1]
        part = stem + tag + ext
    return part


def safe_relpath(key: str) -> str:
    parts = []
    for level in LEVEL_SPLIT.split(key):
        level = level.replace("\\", "/")
        if level.startswith("/") or re.match(r"^[A-Za-z]:", level):
            raise ValueError("absolute path inside archive")
        pieces = [clean_part(p) for p in level.split("/") if p not in ("",)]
        parts.extend(pieces)
    return "/".join(parts)


class Job:
    def __init__(self, top, needed, dest_root, temp_dir, max_temp, have):
        self.top = top
        self.needed = needed            # full member key -> (sha, size)
        self.dest_root = dest_root
        self.temp_dir = temp_dir
        self.max_temp = max_temp
        self.temp_used = 0
        self.have = have                # sha -> path of an already-verified extracted copy
        self.errors: list[str] = []
        self.verified: dict[str, tuple[str, str]] = {}   # key -> (sha, dest path)
        self.need_prefix = set()
        for key in needed:
            for m in re.finditer(r"\.(zip|7z|rar)!", key, re.I):
                self.need_prefix.add(key[: m.end()])

    def dest_for(self, key):
        return os.path.join(self.dest_root, safe_relpath(key))

    def start_member(self, key):
        """Returns (sha object, part path, final path, out file) for writing, or None if already present."""
        expected, _size = self.needed[key]
        final = self.dest_for(key)
        if os.path.exists(final):
            actual = hash_path(final)
            if actual == expected:
                self.verified[key] = (expected, final)
                return None
            raise FileExistsError(f"{final} exists with different content - not overwriting")
        os.makedirs(os.path.dirname(final), exist_ok=True)
        part = final + ".part"
        return hashlib.sha256(), part, final, open(part, "wb")

    def finish_member(self, key, sha, part, final, out):
        out.close()
        expected, _ = self.needed[key]
        if sha.hexdigest() != expected:
            os.unlink(part)
            self.errors.append(f"hash mismatch: {key}")
            return
        os.replace(part, final)
        self.verified[key] = (expected, final)


def hash_path(path):
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            sha.update(block)
    return sha.hexdigest()


def pump(fh, sha, out=None, spool=None):
    while True:
        block = fh.read(CHUNK)
        if not block:
            return
        sha.update(block)
        if out:
            out.write(block)
        if spool:
            spool.write(block)


def run_level(kind, path, prefix, depth, job: Job):
    if kind == "zip":
        level_zip(path, prefix, depth, job)
    elif kind == "7z":
        try:
            level_7z(path, prefix, depth, job)
        except Exception as exc:  # noqa: BLE001
            if "nsupported" not in type(exc).__name__ + str(exc):
                raise
            level_cli(path, prefix, depth, job)
    else:
        level_cli(path, prefix, depth, job)


def take_nested(tmp, kind, key, depth, job):
    try:
        run_level(kind, tmp, key + "!", depth + 1, job)
    except Exception as exc:  # noqa: BLE001
        job.errors.append(f"nested archive {key}: {type(exc).__name__}: {exc}"[:300])
    finally:
        try:
            job.temp_used -= os.path.getsize(tmp)
            os.unlink(tmp)
        except OSError:
            pass


def level_zip(path, prefix, depth, job):
    with zipfile.ZipFile(path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            key = prefix + info.filename
            want = key in job.needed
            nested = (key + "!") in job.need_prefix
            if not (want or nested):
                continue
            started = None
            try:
                with zf.open(info, "r") as fh:
                    spool = tmp = None
                    if nested:
                        fd, tmp = tempfile.mkstemp(dir=job.temp_dir, suffix=os.path.splitext(info.filename)[1])
                        spool = os.fdopen(fd, "wb")
                        job.temp_used += info.file_size
                        if job.temp_used > job.max_temp:
                            raise RuntimeError("temp space limit reached")
                    started = job.start_member(key) if want else None
                    sha = hashlib.sha256() if started is None else started[0]
                    if want and started is None:
                        pass  # already present and verified
                    pump(fh, sha, out=started[3] if started else None, spool=spool)
                    if spool:
                        spool.close()
                    if started:
                        job.finish_member(key, sha, started[1], started[2], started[3])
            except Exception as exc:  # noqa: BLE001
                job.errors.append(f"{key}: {type(exc).__name__}: {exc}"[:300])
                if started:
                    started[3].close()
                    if os.path.exists(started[1]):
                        os.unlink(started[1])
                continue
            if nested and tmp:
                take_nested(tmp, nested_kind(info.filename), key, depth, job)


def level_7z(path, prefix, depth, job):
    import py7zr
    from py7zr.io import Py7zIO, WriterFactory

    with py7zr.SevenZipFile(path, "r") as probe:
        if probe.needs_password():
            raise PermissionError("archive is encrypted (password required)")
    pending = []

    class Sink(Py7zIO):
        def __init__(self, name):
            self.name = name
            self.key = prefix + name
            self.total = 0
            self.mode = "skip"
            self.sha = hashlib.sha256()
            self.out = self.part = self.final = self.spool = self.tmp = None
            try:
                if self.key in job.needed:
                    started = job.start_member(self.key)
                    if started:
                        self.sha, self.part, self.final, self.out = started
                        self.mode = "extract"
                if (self.key + "!") in job.need_prefix:
                    fd, self.tmp = tempfile.mkstemp(dir=job.temp_dir, suffix=os.path.splitext(name)[1])
                    self.spool = os.fdopen(fd, "wb")
                    self.mode = "extract" if self.mode == "extract" else "spool"
            except Exception as exc:  # noqa: BLE001
                job.errors.append(f"{self.key}: {type(exc).__name__}: {exc}"[:300])
                self.mode = "skip"

        def write(self, s):
            b = bytes(s)
            self.total += len(b)
            if self.mode != "skip":
                if self.out:
                    self.sha.update(b)
                    self.out.write(b)
                if self.spool:
                    if job.temp_used + len(b) > job.max_temp:
                        self.spool.close(); os.unlink(self.tmp); self.spool = self.tmp = None
                        job.errors.append(f"{self.key}: temp space limit reached")
                    else:
                        self.spool.write(b)
                        job.temp_used += len(b)
            return len(b)

        def read(self, size=None):
            return b""

        def seek(self, offset, whence=0):
            return 0

        def flush(self):
            pass

        def size(self):
            return self.total

        def done(self):
            if self.spool:
                self.spool.close()
            if self.out:
                job.finish_member(self.key, self.sha, self.part, self.final, self.out)
            if self.tmp and os.path.exists(self.tmp):
                pending.append((self.tmp, self.name))

    sinks = []

    class Factory(WriterFactory):
        def create(self, filename):
            s = Sink(filename)
            sinks.append(s)
            return s

    with py7zr.SevenZipFile(path, "r") as archive:
        archive.extract(factory=Factory())
    for s in sinks:
        s.done()
    for tmp, name in pending:
        take_nested(tmp, nested_kind(name), prefix + name, depth, job)


def level_cli(path, prefix, depth, job):
    """Fallback for rar and 7z methods py7zr cannot decode: the 7z command extracts into a temp dir."""
    work = tempfile.mkdtemp(dir=job.temp_dir)
    try:
        out = subprocess.run(["7z", "x", "-y", f"-o{work}", "--", path], capture_output=True, text=True, timeout=7200)
        if out.returncode not in (0, 1):
            raise RuntimeError((out.stderr or out.stdout).strip()[:200] or f"7z exit {out.returncode}")
        for root, _d, files in os.walk(work):
            for f in files:
                full = os.path.join(root, f)
                key = prefix + os.path.relpath(full, work).replace(os.sep, "/")
                if key in job.needed:
                    try:
                        started = job.start_member(key)
                        if started:
                            sha, part, final, outf = started
                            with open(full, "rb") as fh:
                                pump(fh, sha, out=outf)
                            job.finish_member(key, sha, part, final, outf)
                    except Exception as exc:  # noqa: BLE001
                        job.errors.append(f"{key}: {type(exc).__name__}: {exc}"[:300])
                if (key + "!") in job.need_prefix:
                    take_nested_existing(full, nested_kind(f), key, depth, job)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def take_nested_existing(path, kind, key, depth, job):
    try:
        run_level(kind, path, key + "!", depth + 1, job)
    except Exception as exc:  # noqa: BLE001
        job.errors.append(f"nested archive {key}: {type(exc).__name__}: {exc}"[:300])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--member-hashes", required=True, type=Path)
    ap.add_argument("--scan-db", required=True, type=Path)
    ap.add_argument("--dest-dir", required=True, type=Path)
    ap.add_argument("--temp-dir", required=True, type=Path)
    ap.add_argument("--root", type=Path, help="deletion is refused for archives outside this folder")
    ap.add_argument("--only")
    ap.add_argument("--exclude", action="append", default=[], help="skip archives whose path contains this text (repeatable)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--max-temp-gb", type=float, default=12.0)
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--delete-verified", action="store_true")
    a = ap.parse_args()
    if a.delete_verified and not (a.extract and a.root):
        raise SystemExit("--delete-verified needs --extract and --root")
    root = str(a.root.resolve()) + "/" if a.root else None

    ro = lambda p: sqlite3.connect(f"file:{p}?mode=ro", uri=True)  # noqa: E731
    man, mem, scan = ro(a.manifest), ro(a.member_hashes), ro(a.scan_db)

    def scan_row(path):
        return scan.execute("select size, mtime from files where path=?", (path,)).fetchone()

    def unchanged(path):
        row = scan_row(path)
        try:
            st = os.stat(path)
        except OSError:
            return False
        return bool(row) and st.st_size == row[0] and abs(st.st_mtime - row[1]) <= MTIME_SLACK and os.path.isfile(path)

    # archives to handle
    done = {r[0] for r in mem.execute("select archive_path from archive_done where status='done'")}
    errored = {r[0] for r in mem.execute("select distinct archive_path from member_hashes where error is not null")}
    archives = sorted(done, key=lambda p: (scan_row(p) or (0,))[0])
    gone = [p for p in archives if not os.path.exists(p)]
    if gone:
        print(f"note: {len(gone)} archive(s) no longer on disk (moved or deleted) - skipped", flush=True)
    archives = [p for p in archives if os.path.exists(p) and not any(x in p for x in a.exclude)]
    if a.only:
        archives = [p for p in archives if a.only in p]
    if a.limit:
        archives = archives[: a.limit]

    loose_by_sha = collections.defaultdict(list)
    for sha, loc in man.execute("select sha, location from locations where kind='loose'"):
        loose_by_sha[sha].append(loc)
    opened = set()
    rows_by_top = collections.defaultdict(list)
    for top, mp, size, sha in mem.execute("select archive_path, member_path, size, sha256 from member_hashes where sha256 is not null"):
        rows_by_top[top].append((mp, size, sha))
        if "!" in mp:
            opened.add((top, mp[: mp.rfind("!")]))

    a.dest_dir.mkdir(parents=True, exist_ok=True) if a.extract else None
    log_path = a.dest_dir / "_extract_log.csv"
    have: dict[str, str] = {}
    have_csv = a.dest_dir / "_verified_files.csv"
    if have_csv.exists():
        for sha, size, path in csv.reader(open(have_csv)):
            if os.path.isfile(path) and os.path.getsize(path) == int(size):
                have[sha] = path

    def covered(sha, top):
        for loc in loose_by_sha.get(sha, ()):
            if loc != top and unchanged(loc):
                return loc
        p = have.get(sha)
        return p if p and os.path.isfile(p) else None

    totals = collections.Counter()
    print(f"archives to look at: {len(archives)} | mode: "
          f"{'EXTRACT' if a.extract else 'DRY RUN'}{' + DELETE VERIFIED' if a.delete_verified else ''}", flush=True)
    for n, top in enumerate(archives, 1):
        name = os.path.basename(top)
        leaves = [(mp, size, sha) for mp, size, sha in rows_by_top[top] if size and (top, mp) not in opened]
        needed = {}
        for mp, size, sha in leaves:
            if not covered(sha, top):
                needed[mp] = (sha, size)
        nbytes = sum(v[1] for v in needed.values())
        verdict, deleted = "", False
        if top in errored:
            verdict = "KEEP: some files inside could not be read earlier (password/damage)"
        elif not needed:
            verdict = "all content already exists elsewhere"
        else:
            verdict = f"needs extraction of {len(needed):,} files ({nbytes/1e9:.2f} GB)"
        if top in errored or (not a.extract):
            print(f"[{n}/{len(archives)}] {name[:60]} -> {verdict}", flush=True)
            totals["kept" if top in errored else ("skip_covered" if not needed else "would_extract")] += 1
            continue

        ok = True
        if needed:
            free = shutil.disk_usage(a.dest_dir).free
            if free < nbytes * 1.05 + 2e9:
                print(f"[{n}] STOP: not enough free space at destination", flush=True)
                break
            dest_root = os.path.join(a.dest_dir, f"{os.path.splitext(name)[0]}-{hashlib.sha1(top.encode()).hexdigest()[:6]}")
            a.temp_dir.mkdir(parents=True, exist_ok=True)
            job = Job(top, needed, dest_root, str(a.temp_dir), int(a.max_temp_gb * 1e9), have)
            started = time.time()
            ext = os.path.splitext(top.lower())[1]
            try:
                run_level({".zip": "zip", ".7z": "7z"}.get(ext, "rar"), top, "", 0, job)
            except Exception as exc:  # noqa: BLE001
                job.errors.append(f"{type(exc).__name__}: {exc}"[:300])
            missing = [k for k in needed if k not in job.verified]
            with open(have_csv, "a", newline="") as fh:
                w = csv.writer(fh)
                for key, (sha, dpath) in job.verified.items():
                    w.writerow([sha, needed[key][1], dpath])
                    have[sha] = dpath
            ok = not job.errors and not missing
            verdict = (f"extracted+verified {len(job.verified):,}/{len(needed):,} files in {time.time()-started:.0f}s"
                       + ("" if ok else f" | PROBLEMS: {len(missing)} missing, {len(job.errors)} errors, e.g. {(job.errors or ['-'])[0][:120]}"))
        # final re-check of everything that is supposed to exist elsewhere
        if ok and a.delete_verified:
            everything = all(covered(sha, top) for _mp, _s, sha in leaves)
            safe_to_delete = (everything and unchanged(top) and root and top.startswith(root)
                              and os.path.isfile(top) and not os.path.islink(top))
            if safe_to_delete:
                os.unlink(top)
                deleted = True
            else:
                verdict += " | archive KEPT (final safety check failed)"
        print(f"[{n}/{len(archives)}] {name[:60]} -> {verdict}{' | ARCHIVE DELETED' if deleted else ''}", flush=True)
        totals["deleted" if deleted else ("extracted_ok" if ok else "problems")] += 1
        with open(log_path, "a", newline="") as fh:
            csv.writer(fh).writerow([time.strftime("%F %T"), top, verdict, int(deleted)])
    print("SUMMARY:", dict(totals))


if __name__ == "__main__":
    main()

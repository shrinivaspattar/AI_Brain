#!/usr/bin/env python3
"""Compare files and whole folders by content and write a manifest of the
UNIQUE data. Works only on checksum files already made by the other discovery
scripts (scan, duplicate report, loose-file hashes, archive-member hashes); it
never touches the scanned drive and copies, moves or deletes nothing.

Identity of a file = its SHA-256. A loose file gets it from (in order) its own
checksum, else its duplicate-report group's checksum, else it has no known twin
and is treated as unique. Files inside archives (including nested archives)
come from the member-hash file.

Folders are compared by a Merkle fingerprint (names + content of everything
inside, ignoring empty folders): two folders with the same fingerprint hold
exactly the same files under the same names, whether they are loose folders,
folders inside an archive, or the root of an archive / nested archive.

Outputs (SQLite + a plain-text summary): contents, locations, folder_groups,
folder_members.

Usage:
    python scripts/build_unique_manifest.py --scan-db scan.sqlite \\
        --dup-report duplicate_analysis.json --loose-hashes loose.sqlite \\
        --member-hashes members.sqlite --out manifest.sqlite --summary summary.txt
"""

import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

EMPTY_SHA = hashlib.sha256(b"").hexdigest()
LEVEL_SPLIT = re.compile(r"(?<=\.zip!)|(?<=\.7z!)|(?<=\.rar!)", re.I)
ARCHIVE_EXTS = (".zip", ".7z", ".rar")

SCHEMA = """
CREATE TABLE contents (sha TEXT PRIMARY KEY, size INTEGER, ext TEXT, is_container INTEGER,
                       n_loose INTEGER, n_archive INTEGER, known INTEGER);
CREATE TABLE locations (sha TEXT, kind TEXT, location TEXT, size INTEGER);
CREATE TABLE folder_groups (group_id INTEGER PRIMARY KEY, files INTEGER, bytes INTEGER, copies INTEGER,
                            reclaimable_bytes INTEGER, maximal INTEGER, fingerprint TEXT);
CREATE TABLE folder_members (group_id INTEGER, kind TEXT, location TEXT);
"""


def h(*parts) -> str:
    m = hashlib.sha1()
    for p in parts:
        m.update(p.encode("utf-8", "replace"))
        m.update(b"\0")
    return m.hexdigest()


class Tree:
    """Merkle fingerprints over a set of (node path -> entries)."""

    def __init__(self):
        self.files = collections.defaultdict(list)   # dir -> [(name, sha, size)]
        self.dirs = collections.defaultdict(set)     # dir -> {child dir}

    def add(self, dirpath, name, sha, size):
        self.files[dirpath].append((name, sha, size))
        node = dirpath
        while True:
            parent = node.rsplit("/", 1)[0] if "/" in node else None
            if parent is None or node in self.dirs.get(parent, ()):
                break
            if parent is None:
                break
            self.dirs[parent].add(node)
            node = parent

    def fingerprints(self, roots_only_below=None):
        """-> {dir: (fingerprint, n_files, n_bytes)} for non-empty dirs."""
        out = {}
        for d in sorted(set(self.files) | set(self.dirs), key=lambda p: -p.count("/")):
            entries = []
            nfiles = nbytes = 0
            for name, sha, size in self.files.get(d, ()):
                entries.append("f\0" + name + "\0" + sha)
                nfiles += 1
                nbytes += size
            for child in self.dirs.get(d, ()):
                if child in out:
                    fp, cf, cb = out[child]
                    entries.append("d\0" + child.rsplit("/", 1)[-1] + "\0" + fp)
                    nfiles += cf
                    nbytes += cb
            if nfiles:
                entries.sort()
                out[d] = (h(*entries), nfiles, nbytes)
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan-db", required=True, type=Path)
    ap.add_argument("--dup-report", required=True, type=Path)
    ap.add_argument("--loose-hashes", required=True, type=Path)
    ap.add_argument("--member-hashes", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--summary", required=True, type=Path)
    a = ap.parse_args()
    if a.out.exists():
        raise SystemExit(f"{a.out} exists - refusing to overwrite")

    ro = lambda p: sqlite3.connect(f"file:{p}?mode=ro", uri=True)  # noqa: E731
    scan, loose_db, mem = ro(a.scan_db), ro(a.loose_hashes), ro(a.member_hashes)
    report = json.load(open(a.dup_report))
    analyzed_at = datetime.datetime.fromisoformat(report["analyzed_at"]).timestamp()
    d1 = {}
    for g in report["exact_duplicate_groups"]:
        for p in g["paths"]:
            d1[p] = (g["content_hash"], g["size_bytes"])
    own = {p: s for p, s in loose_db.execute("select path, sha256 from loose_hashes where sha256 is not null")}

    # ---- loose files -> sha
    loose = []   # (path, size, sha, known)
    n_unknown = 0
    scan_paths = set()
    for path, size, mtime in scan.execute("select path, size, mtime from files"):
        scan_paths.add(path)
        if size == 0:
            sha, known = EMPTY_SHA, 1
        elif path in own:
            sha, known = own[path], 1
        elif path in d1 and d1[path][1] == size and mtime <= analyzed_at:
            sha, known = d1[path][0], 1
        else:
            sha, known = "unique:" + path, 0
            n_unknown += 1
        loose.append((path, size, sha, known))
    missing_from_scan = sum(1 for p in d1 if p not in scan_paths)

    # safety net: a file with no known twin must not share its size with a file checksummed after the report
    late_sizes = set()
    for path, size, mtime in scan.execute("select path, size, mtime from files where mtime > ?", (analyzed_at,)):
        if size:
            late_sizes.add(size)
    unresolved = sum(1 for (p, s, sha, k) in loose if not k and s in late_sizes)

    # ---- archive members -> sha
    members = mem.execute("select archive_path, member_path, size, sha256 from member_hashes "
                          "where sha256 is not null").fetchall()
    opened = set()  # (top, member_path of a nested archive that has an expansion)
    for top, mp, _s, _h in members:
        if "!" in mp:
            opened.add((top, mp[: mp.rfind("!")]))
    top_archives = {r[0] for r in mem.execute("select archive_path from archive_done where status='done'")}

    # ---- contents / locations
    out = sqlite3.connect(a.out)
    out.executescript(SCHEMA)
    contents: dict[str, list] = {}   # sha -> [size, ext, is_container, n_loose, n_archive, known]
    locs = []

    def note(sha, size, name, is_container, kind, location, known):
        c = contents.get(sha)
        if c is None:
            c = contents[sha] = [size, os.path.splitext(name.lower())[1], int(is_container), 0, 0, known]
        c[3 if kind == "loose" else 4] += 1
        c[2] = c[2] or int(is_container)
        locs.append((sha, kind, location, size))

    for path, size, sha, known in loose:
        note(sha, size, path, path in top_archives, "loose", path, known)
    for top, mp, size, sha in members:
        note(sha, size, mp, (top, mp) in opened, "archive", top + " :: " + mp, 1)
    out.executemany("INSERT INTO contents VALUES (?,?,?,?,?,?,?)",
                    [(k, v[0], v[1], v[2], v[3], v[4], v[5]) for k, v in contents.items()])
    out.executemany("INSERT INTO locations VALUES (?,?,?,?)", locs)
    out.commit()

    # ---- folder fingerprints
    nodes = {}   # node id -> (fp, files, bytes, label, kind)
    parents = {}  # node id -> parent node id (none for roots)
    loose_tree = Tree()
    for path, size, sha, known in loose:
        d, name = path.rsplit("/", 1)
        loose_tree.add(d, name, sha, size)
    for d, (fp, nf, nb) in loose_tree.fingerprints().items():
        nodes["L|" + d] = (fp, nf, nb, d, "loose folder")
        parents["L|" + d] = "L|" + d.rsplit("/", 1)[0]

    levels = collections.defaultdict(Tree)   # (top, level prefix) -> Tree
    for top, mp, size, sha in members:
        parts = LEVEL_SPLIT.split(mp)
        prefix, rest = "".join(parts[:-1]), parts[-1]
        d, _, name = rest.rpartition("/")
        levels[(top, prefix)].add(d, name, sha, size)
    for (top, prefix), tree in levels.items():
        for d, (fp, nf, nb) in tree.fingerprints().items():
            label = top + " :: " + prefix + d
            kind = "archive root" if d == "" else "folder inside archive"
            nodes["A|" + label] = (fp, nf, nb, label, kind)
            if d:
                parents["A|" + label] = "A|" + top + " :: " + prefix + (d.rsplit("/", 1)[0] if "/" in d else "")

    by_fp = collections.defaultdict(list)
    for nid, (fp, nf, nb, label, kind) in nodes.items():
        by_fp[fp].append(nid)

    def parent_fp(nid):
        parent = parents.get(nid)
        return nodes[parent][0] if parent in nodes else None

    gid = 0
    rows_g, rows_m = [], []
    total_reclaim = 0
    for fp, ids in by_fp.items():
        if len(ids) < 2:
            continue
        _fp, nf, nb, _l, _k = nodes[ids[0]]
        parent_fps = {parent_fp(i) for i in ids}
        maximal = 0 if (len(parent_fps) == 1 and None not in parent_fps) else 1
        gid += 1
        reclaim = nb * (len(ids) - 1)
        rows_g.append((gid, nf, nb, len(ids), reclaim, maximal, fp))
        for i in ids:
            rows_m.append((gid, nodes[i][4], nodes[i][3]))
        if maximal:
            total_reclaim += reclaim
    out.executemany("INSERT INTO folder_groups VALUES (?,?,?,?,?,?,?)", rows_g)
    out.executemany("INSERT INTO folder_members VALUES (?,?,?)", rows_m)
    out.commit()

    # ---- summary
    real = {k: v for k, v in contents.items() if not v[2]}     # exclude opened archive containers
    def tot(pred):
        sel = [v for v in real.values() if pred(v)]
        return len(sel), sum(v[0] for v in sel)
    both = tot(lambda v: v[3] and v[4])
    lo = tot(lambda v: v[3] and not v[4])
    ao = tot(lambda v: v[4] and not v[3])
    all_ = tot(lambda v: True)
    by_ext = collections.Counter()
    for v in real.values():
        by_ext[v[1] or "(none)"] += v[0]
    lines = [
        "UNIQUE DATA MANIFEST (exact content, SHA-256)", "",
        f"loose files: {len(loose):,} | files inside archives (all levels): {len(members):,}",
        f"distinct contents (archive containers counted via their members): {all_[0]:,} = {all_[1]/1e9:,.1f} GB",
        f"  present as loose file only:         {lo[0]:>9,} = {lo[1]/1e9:8.1f} GB",
        f"  present inside archives only:       {ao[0]:>9,} = {ao[1]/1e9:8.1f} GB",
        f"  present both loose and in archive:  {both[0]:>9,} = {both[1]/1e9:8.1f} GB",
        f"  (loose files with no known twin, treated as unique: {n_unknown:,})",
        f"  same-size doubt (unhashed file sharing a size with a file changed after the report): {unresolved:,}",
        f"  duplicate-report paths not found in the scan: {missing_from_scan:,}",
        "", f"identical-folder groups: {len(rows_g):,}; top-level (maximal) ones: {sum(r[5] for r in rows_g):,}; "
        f"folder bytes that repeat: {total_reclaim/1e9:,.1f} GB", "",
        "Biggest maximal identical-folder groups (by repeated bytes):",
    ]
    for gr in sorted((r for r in rows_g if r[5]), key=lambda r: -r[4])[:25]:
        lines.append(f"  group {gr[0]}: {gr[3]} copies x {gr[2]/1e9:.2f} GB, {gr[1]:,} files")
        for kind, loc in [(m[1], m[2]) for m in rows_m if m[0] == gr[0]][:4]:
            lines.append(f"     [{kind}] {loc}")
    lines += ["", "Distinct-content GB by file type (top 15):"]
    size_by_ext = collections.Counter()
    for v in real.values():
        size_by_ext[v[1] or "(none)"] += v[0]
    for ext, b in size_by_ext.most_common(15):
        lines.append(f"  {ext:12s} {b/1e9:8.1f} GB")
    a.summary.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:14]))


if __name__ == "__main__":
    main()

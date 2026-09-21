#!/usr/bin/env python3
"""Plan-only: from a FULL hash of a drive (hash_all_files.py), decide one copy to
keep per distinct content and list which folders / single files hold only
repeat copies. Reads local SQLite files only; deletes nothing.

Copies of archives that were already processed and verified (their SHA-256 is
in --processed-archives-sha) are treated as removable: their contents live on
in extracted or loose files. Empty files are ignored.

Usage:
    python scripts/build_removal_plan_from_hashes.py --hashes all.sqlite --manifest m.sqlite \\
        --member-hashes h.sqlite --out-dir DIR --root /path/to/scanned/root [--avoid text ...]
"""

import argparse
import collections
import csv
import re
import sqlite3
from pathlib import Path

DIRTY_NAME = re.compile(r"(_\d{10}(\.[^./]+)?$)|(\.duplicate-\d+)|( \(\d+\)(\.[^./]+)?$)|( - copy)| copy(\.[^./]+)?$", re.I)


def rank(path, avoid):
    low = path.lower()
    name = path.rsplit("/", 1)[-1]
    return (sum(a in low for a in avoid), int(bool(DIRTY_NAME.search(name))), path.count("/"), len(path), path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hashes", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--member-hashes", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--root", required=True)
    ap.add_argument("--avoid", action="append", default=[])
    a = ap.parse_args()
    avoid = [x.lower() for x in a.avoid]
    a.out_dir.mkdir(parents=True, exist_ok=True)
    ro = lambda p: sqlite3.connect(f"file:{p}?mode=ro", uri=True)  # noqa: E731
    H, M, D = ro(a.hashes), ro(a.manifest), ro(a.member_hashes)

    # SHA-256 of archives that were opened and verified (and had no unreadable members)
    ok_archives = {r[0] for r in D.execute("select archive_path from archive_done where status='done'")}
    bad = {r[0] for r in D.execute("select distinct archive_path from member_hashes where error is not null")}
    good = ok_archives - bad
    verified_archive_sha = set()
    for sha, loc in M.execute("select sha, location from locations where kind='loose'"):
        if loc in good:
            verified_archive_sha.add(sha)

    by_sha = collections.defaultdict(list)
    size_of = {}
    for path, size, sha in H.execute("select path, size, sha256 from all_hashes where sha256 is not null"):
        by_sha[sha].append(path)
        size_of[path] = size
    keep = set()
    removable_archive_files = 0
    for sha, paths in by_sha.items():
        if sha in verified_archive_sha:
            removable_archive_files += len(paths)
            continue                       # no copy needs to be kept: the contents were extracted / covered
        keep.add(min(paths, key=lambda p: rank(p, avoid)))

    nbytes = collections.Counter(); nfiles = collections.Counter(); nkeep = collections.Counter()
    for p, s in size_of.items():
        d = p
        while "/" in d:
            d = d.rsplit("/", 1)[0]
            nbytes[d] += s; nfiles[d] += 1
            if p in keep:
                nkeep[d] += 1
    removable = {d for d in nbytes if nkeep[d] == 0}
    maximal = [d for d in removable if d.rsplit("/", 1)[0] not in removable and d.startswith(a.root)]
    pref = tuple(m + "/" for m in maximal)
    singles = [(p, s) for p, s in size_of.items() if p not in keep and not p.startswith(pref)]

    with open(a.out_dir / "removable_folders.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["folder", "files", "bytes"])
        for d in sorted(maximal, key=lambda d: -nbytes[d]):
            w.writerow([d, nfiles[d], nbytes[d]])
    with open(a.out_dir / "removable_single_files.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["file", "bytes"])
        for p, s in sorted(singles, key=lambda x: -x[1]):
            w.writerow([p, s])
    with open(a.out_dir / "keep_files.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["file", "bytes"])
        for p in sorted(keep):
            w.writerow([p, size_of[p]])
    total = sum(size_of.values())
    kept = sum(size_of[p] for p in keep)
    cov = sum(nbytes[d] for d in maximal)
    ssum = sum(s for _p, s in singles)
    lines = [
        "REMOVAL PLAN FROM FULL HASHES (a plan only - nothing deleted)", "",
        f"files hashed: {len(size_of):,} = {total/1e9:,.1f} GB",
        f"distinct contents to keep: {len(keep):,} = {kept/1e9:,.1f} GB (one preferred copy each)",
        f"processed archive files that need no copy: {removable_archive_files:,}",
        f"folders removable as a whole: {len(maximal):,} = {cov/1e9:,.1f} GB",
        f"other single removable files: {len(singles):,} = {ssum/1e9:,.1f} GB",
        f"total that can be removed: {(cov+ssum)/1e9:,.1f} GB  -> would leave about {(total-cov-ssum)/1e9:,.1f} GB",
        "", "Largest removable folders:",
    ]
    for d in sorted(maximal, key=lambda d: -nbytes[d])[:25]:
        lines.append(f"  {nbytes[d]/1e9:7.1f} GB {nfiles[d]:>8,} files  {d}")
    (a.out_dir / "summary.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Plan-only: which folders and files on the scanned drive could be removed
WITHOUT losing any unique content, given keep_list.csv (one preferred copy per
distinct content). A folder is "fully removable" when none of the kept copies
live inside it (loose kept files inside it, or kept members of an archive
inside it). Reads only local SQLite/CSV files; deletes nothing anywhere.

Usage:
    python scripts/build_free_space_plan.py --scan-db scan.sqlite --keep-list keep_list.csv \\
        --archives-plan archives_plan.csv --out-dir DIR
"""

import argparse
import collections
import csv
import sqlite3
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan-db", required=True, type=Path)
    ap.add_argument("--keep-list", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    a = ap.parse_args()

    keeps = set()          # loose paths that are the kept copy, or archives holding a kept member
    for r in csv.DictReader(open(a.keep_list)):
        loc = r["keep_location"]
        keeps.add(loc.split(" :: ", 1)[0])
    scan = sqlite3.connect(f"file:{a.scan_db}?mode=ro", uri=True)

    nbytes = collections.Counter()
    nfiles = collections.Counter()
    nkeep = collections.Counter()
    file_rows = []
    for path, size in scan.execute("select path, size from files"):
        file_rows.append((path, size))
        d = path
        while "/" in d:
            d = d.rsplit("/", 1)[0]
            nbytes[d] += size
            nfiles[d] += 1
            if path in keeps:
                nkeep[d] += 1

    dirs = [d for d in nbytes]
    removable = [d for d in dirs if nkeep[d] == 0]
    rem_set = set(removable)
    maximal = [d for d in removable if d.rsplit("/", 1)[0] not in rem_set]
    covered = sum(nbytes[d] for d in maximal)
    # loose files not inside a maximal removable folder, and not kept
    under = lambda p: any(p.startswith(m + "/") for m in ())  # noqa: E731
    max_prefix = tuple(m + "/" for m in maximal)
    loose_rest = [(p, s) for p, s in file_rows if p not in keeps and not p.startswith(max_prefix)]

    a.out_dir.mkdir(parents=True, exist_ok=True)
    with open(a.out_dir / "free_space_folders.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["folder", "files", "bytes"])
        for d in sorted(maximal, key=lambda d: -nbytes[d]):
            w.writerow([d, nfiles[d], nbytes[d]])
    with open(a.out_dir / "free_space_single_files.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "bytes"])
        for p, s in sorted(loose_rest, key=lambda x: -x[1]):
            w.writerow([p, s])
    total = sum(s for _p, s in file_rows)
    kept_bytes = sum(s for p, s in file_rows if p in keeps)
    lines = [
        "FREE-SPACE PLAN (nothing deleted; a plan for a person to review)", "",
        f"data on the scanned drive: {total/1e9:,.1f} GB in {len(file_rows):,} files",
        f"folders fully removable without losing unique content: {len(maximal):,} = {covered/1e9:,.1f} GB",
        f"other single files that are repeat copies (not inside those folders): {len(loose_rest):,} = {sum(s for _p, s in loose_rest)/1e9:,.1f} GB",
        f"kept (must stay): {kept_bytes/1e9:,.1f} GB in loose files/archives that hold the preferred copy",
        "", "Largest removable folders:",
    ]
    for d in sorted(maximal, key=lambda d: -nbytes[d])[:25]:
        lines.append(f"  {nbytes[d]/1e9:7.1f} GB  {nfiles[d]:>8,} files  {d}")
    (a.out_dir / "free_space_summary.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

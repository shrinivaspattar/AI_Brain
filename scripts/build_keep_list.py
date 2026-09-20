#!/usr/bin/env python3
"""Turn unique_manifest.sqlite (from build_unique_manifest.py) into a PLAN:
one preferred location for every distinct content, which archives add nothing
new, and which identical-folder groups have a preferred copy. It only reads the
manifest and writes CSV/text files: nothing on any drive is copied, moved or
deleted.

Preference for the copy to keep (best first): a loose file over one inside an
archive; not in a location matching --avoid (repeatable, case-insensitive
substring); a clean name (no `_<10-digit>` timestamp suffix, no `.duplicate-N`,
no ` (1)`/` copy`); shallower; shorter path.

Usage:
    python scripts/build_keep_list.py --manifest manifest.sqlite --out-dir DIR \\
        [--avoid quarantine --avoid text ...]
"""

import argparse
import collections
import csv
import re
import sqlite3
from pathlib import Path

DIRTY_NAME = re.compile(r"(_\d{10}(\.[^./]+)?$)|(\.duplicate-\d+)|( \(\d+\)(\.[^./]+)?$)|( - copy)| copy(\.[^./]+)?$", re.I)
JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


def rank(location: str, avoid: list[str], is_archive: bool):
    low = location.lower()
    name = location.rsplit("/", 1)[-1]
    return (int(is_archive), sum(a in low for a in avoid), int(bool(DIRTY_NAME.search(name))),
            location.count("/"), len(location), location)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--avoid", action="append", default=[])
    a = ap.parse_args()
    avoid = [x.lower() for x in a.avoid]
    a.out_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(f"file:{a.manifest}?mode=ro", uri=True)

    info = {sha: (size, cont, nl, na) for sha, size, cont, nl, na in
            db.execute("select sha,size,is_container,n_loose,n_archive from contents")}
    best: dict[str, tuple] = {}
    archive_files = collections.defaultdict(list)   # top archive -> [sha]
    archive_size = {}
    for sha, kind, loc, size in db.execute("select sha,kind,location,size from locations"):
        r = rank(loc, avoid, kind == "archive")
        if sha not in best or r < best[sha][0]:
            best[sha] = (r, kind, loc)
        if kind == "archive":
            archive_files[loc.split(" :: ", 1)[0]].append(sha)
    for sha, kind, loc, size in db.execute("select sha,kind,location,size from locations where kind='loose'"):
        if loc in archive_files:
            archive_size[loc] = size

    kept = extra = 0
    junk = 0
    with open(a.out_dir / "keep_list.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sha256", "size_bytes", "keep_kind", "keep_location", "copies_loose", "copies_in_archives", "looks_like_junk"])
        for sha, (r, kind, loc) in best.items():
            size, cont, nl, na = info[sha]
            if cont:
                continue
            is_junk = int(loc.rsplit("/", 1)[-1].lower() in JUNK_NAMES)
            junk += is_junk
            kept += size
            w.writerow([sha, size, kind, loc, nl, na, is_junk])

    red = []
    n_unverified = 0
    for top, shas in archive_files.items():
        real = [s for s in shas if not info[s][1] and info[s][0] > 0]
        only = [s for s in set(real) if info[s][2] == 0]
        red.append((top, archive_size.get(top, 0), len(real), len(only), sum(info[s][0] for s in only)))
    with open(a.out_dir / "archives_plan.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["archive", "archive_bytes", "files_inside", "files_found_nowhere_else_loose", "bytes_found_nowhere_else_loose", "verdict"])
        for top, sz, n, no, nb in sorted(red, key=lambda r: -r[1]):
            w.writerow([top, sz, n, no, nb, "adds nothing new (all content also loose)" if no == 0 else "holds content not loose elsewhere"])
    zero = [r for r in red if r[3] == 0]
    some = [r for r in red if r[3] > 0]

    grp = db.execute("select group_id,files,bytes,copies from folder_groups where maximal=1 order by reclaimable_bytes desc").fetchall()
    members = collections.defaultdict(list)
    for gid, kind, loc in db.execute("select group_id,kind,location from folder_members"):
        members[gid].append((kind, loc))
    total_loose = db.execute("select coalesce(sum(size),0) from locations where kind='loose'").fetchone()[0]
    with open(a.out_dir / "folder_plan.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["group_id", "files", "bytes", "copies", "role", "kind", "location"])
        for gid, files, b, copies in grp:
            ms = sorted(members[gid], key=lambda m: rank(m[1], avoid, m[0] != "loose folder"))
            for i, (kind, loc) in enumerate(ms):
                w.writerow([gid, files, b, copies, "KEEP" if i == 0 else "redundant copy", kind, loc])

    lines = [
        "KEEP-LIST PLAN (nothing has been changed on any drive)", "",
        f"unique contents to keep: {sum(1 for s in best if not info[s][1]):,} = {kept/1e9:,.1f} GB",
        f"loose data today (apparent size): {total_loose/1e9:,.1f} GB",
        f"  of which is NOT needed to keep every unique file (repeat copies + the archive files themselves): about {(total_loose - sum(info[s][0] for s,(r,k,l) in best.items() if k=='loose' and not info[s][1]))/1e9:,.1f} GB",
        f"archives adding nothing new (everything inside is also loose): {len(zero)} = {sum(r[1] for r in zero)/1e9:,.1f} GB",
        f"archives holding content found nowhere else loose: {len(some)} = {sum(r[1] for r in some)/1e9:,.1f} GB "
        f"(they hold {sum(r[3] for r in some):,} such files, {sum(r[4] for r in some)/1e9:,.1f} GB)",
        f"identical-folder groups with a preferred copy: {len(grp):,}",
        f"junk-named files among the kept (Thumbs.db etc.): {junk:,}",
        "", "Files written: keep_list.csv, archives_plan.csv, folder_plan.csv",
    ]
    (a.out_dir / "keep_plan_summary.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

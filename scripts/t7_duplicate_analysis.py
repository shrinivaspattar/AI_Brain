#!/usr/bin/env python3
"""Run a read-only T7 duplicate analysis (Phase D1) and write a JSON
report.

Usage:
    python scripts/t7_duplicate_analysis.py <root> <output.json>

Performs ONLY the read-only analysis implemented in
`app.discovery.duplicate_analysis.analyze_duplicates` - it never
writes, moves, renames, deletes, extracts, or quarantines anything
under `<root>`. It DOES read file content (to compute a SHA-256 hash)
for files that share a size with at least one other file - that is
exactly what "computing hashes for duplicate detection" was authorized
to mean. See "T7 Deduplication Analysis" in AI_Brain_Architecture.md.
"""

import argparse
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.discovery.duplicate_analysis import analyze_duplicates, write_duplicate_report  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Directory to analyze (read-only)")
    parser.add_argument("output", type=Path, help="Path to write the JSON report to")
    args = parser.parse_args()

    print(f"Analyzing {args.root} (read-only; hashes files with a size collision)...", flush=True)
    started = time.monotonic()
    analysis = analyze_duplicates(args.root)
    elapsed = time.monotonic() - started

    written = write_duplicate_report(analysis, args.output)

    print(f"Done in {elapsed:.1f}s.")
    print(f"Files considered: {analysis.total_files_considered:,}")
    print(f"Directories considered: {analysis.total_directories_considered:,}")
    print(
        f"Size-collision candidates: {analysis.size_collision_candidate_files:,} files, "
        f"{analysis.size_collision_candidate_bytes:,} bytes"
    )
    print(f"Files hashed: {analysis.files_hashed:,}")
    print(f"Exact duplicate groups: {len(analysis.exact_duplicate_groups):,}")
    print(f"Cryptomator chunk duplicate groups: {len(analysis.cryptomator_chunk_duplicate_groups):,}")
    print(f"Directory duplicate groups: {len(analysis.directory_duplicate_groups):,}")
    print(f"Reclaimable (exact file duplicates): {analysis.exact_duplicate_reclaimable_bytes:,} bytes")
    print(f"Reclaimable (duplicate directory trees): {analysis.directory_duplicate_reclaimable_bytes:,} bytes")
    print(f"Scan errors: {len(analysis.errors)}  Hash errors: {len(analysis.hash_errors)}")
    print(f"Report written to: {written}")


if __name__ == "__main__":
    main()

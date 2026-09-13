#!/usr/bin/env python3
"""Run a read-only corpus inventory scan and write a JSON report.

Usage:
    python scripts/t7_discovery.py <root> <output.json> [--top-n N]

This script performs ONLY the read-only scan implemented in
`app.discovery.corpus_inventory.scan_corpus` - it never writes, moves,
renames, deletes, extracts, or hashes anything under `<root>`. See
"T7 Corpus Discovery / Inventory" in AI_Brain_Architecture.md for the
milestone this script exists for.
"""

import argparse
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.discovery.corpus_inventory import scan_corpus, write_inventory_report  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Directory to scan (read-only)")
    parser.add_argument("output", type=Path, help="Path to write the JSON report to")
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="How many largest files/directories to record (default: 20)",
    )
    args = parser.parse_args()

    print(f"Scanning {args.root} (read-only, metadata only)...", flush=True)
    started = time.monotonic()
    inventory = scan_corpus(args.root, top_n=args.top_n)
    elapsed = time.monotonic() - started

    written = write_inventory_report(inventory, args.output)

    print(f"Done in {elapsed:.1f}s.")
    print(f"Files: {inventory.total_files:,}")
    print(f"Directories: {inventory.total_directories:,}")
    print(f"Total size: {inventory.total_size_bytes:,} bytes")
    print(f"Errors encountered: {len(inventory.errors)}")
    print(f"Report written to: {written}")


if __name__ == "__main__":
    main()

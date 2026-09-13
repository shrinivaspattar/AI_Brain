#!/usr/bin/env python3
"""Run the read-only T7 Provenance-Aware Duplicate Analysis (Phase D2)
and write a compact JSON report plus a human-readable summary.

Usage (fresh run against a D1 report - touches the T7 via os.lstat):
    python scripts/t7_provenance_analysis.py collect <d1_report.json> <output.json> <summary.txt>

Usage (reclassify an existing D2 report after a logic/schema change -
zero filesystem access, since the raw facts are already collected):
    python scripts/t7_provenance_analysis.py reclassify <previous_report.json> <d1_report.json> <output.json> <summary.txt>

See "T7 Provenance-Aware Duplicate Analysis (Phase D2)" in
AI_Brain_Architecture.md.
"""

import argparse
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.discovery.provenance_analysis import (  # noqa: E402
    INFERENCE_PROSE,
    classify,
    collect_path_signals,
    load_raw_from_previous_report,
    write_provenance_report,
)


def _write_human_summary(analysis, destination: Path) -> None:
    lines: list[str] = []
    lines.append("T7 Provenance-Aware Duplicate Analysis (Phase D2) - Summary")
    lines.append("=" * 70)
    lines.append("")
    lines.append(analysis.to_json_dict()["_read_this_first"])
    lines.append("")
    lines.append(f"D1 root analyzed: {analysis.d1_root}")
    lines.append(f"D1 analyzed at: {analysis.d1_analyzed_at}")
    lines.append(f"D2 analyzed at: {analysis.d2_analyzed_at}")
    lines.append("")
    lines.append(
        f"Paths named by D1 that no longer existed when D2 checked: "
        f"{analysis.paths_no_longer_existing}"
    )
    lines.append("")
    lines.append("Inference code counts (across all group types):")
    for code, count in sorted(analysis.inference_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {code}: {count}")
    lines.append("")
    lines.append("Confidence counts:")
    for confidence, count in sorted(analysis.confidence_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {confidence}: {count}")
    lines.append("")

    for kind, groups, note in (
        ("Exact duplicate", analysis.exact_duplicate_provenance, None),
        (
            "Cryptomator chunk duplicate",
            analysis.cryptomator_chunk_provenance,
            "NOTE: .c9r matches are CIPHERTEXT chunk matches, not plaintext "
            "document matches - do not read these the same way as exact "
            "duplicates above.",
        ),
        ("Directory-structural duplicate", analysis.directory_duplicate_provenance, None),
    ):
        lines.append("-" * 70)
        lines.append(f"{kind} groups: {len(groups)} total")
        if note:
            lines.append(note)
        top = sorted(groups, key=lambda g: -g.reclaimable_bytes)[:10]
        for g in top:
            lines.append("")
            lines.append(
                f"  [{g.inference_code} | confidence={g.confidence} | "
                f"reclaimable={g.reclaimable_bytes:,} bytes | "
                f"review_needed={g.requires_human_review}]"
            )
            for p in g.paths[:5]:
                lines.append(f"    - {p}")
            if len(g.paths) > 5:
                lines.append(f"    ... and {len(g.paths) - 5} more")
            lines.append(f"    FACTS: {g.structured_facts}")
            lines.append(f"    EVIDENCE: {g.evidence_codes}")
            lines.append(f"    INFERENCE: {INFERENCE_PROSE[g.inference_code]}")
        lines.append("")

    destination.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)

    collect_parser = subparsers.add_parser("collect", help="Fresh run against a D1 report")
    collect_parser.add_argument("d1_report", type=Path)
    collect_parser.add_argument("output", type=Path)
    collect_parser.add_argument("summary", type=Path)

    reclassify_parser = subparsers.add_parser(
        "reclassify", help="Reclassify an existing D2 report - zero filesystem access"
    )
    reclassify_parser.add_argument("previous_report", type=Path)
    reclassify_parser.add_argument("d1_report", type=Path)
    reclassify_parser.add_argument("output", type=Path)
    reclassify_parser.add_argument("summary", type=Path)

    args = parser.parse_args()

    started = time.monotonic()
    if args.mode == "collect":
        print(f"Collecting provenance facts from {args.d1_report} (read-only os.lstat)...", flush=True)
        raw = collect_path_signals(args.d1_report)
    else:
        print(
            f"Reclassifying {args.previous_report} - zero filesystem access...",
            flush=True,
        )
        raw = load_raw_from_previous_report(args.previous_report, args.d1_report)

    analysis = classify(raw)
    elapsed = time.monotonic() - started

    written = write_provenance_report(analysis, args.output)
    _write_human_summary(analysis, args.summary)

    print(f"Done in {elapsed:.1f}s.")
    print(f"Inference counts: {analysis.inference_counts}")
    print(f"Confidence counts: {analysis.confidence_counts}")
    print(f"Paths no longer existing: {analysis.paths_no_longer_existing}")
    print(f"JSON report written to: {written}")
    print(f"Human-readable summary written to: {args.summary}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Quality filter for OCR'd lecture screen text (extract_lecture_screen_text.py).

WHY: OCR of typed slides is decent, but terminals, VM desktops and small UI
text produce junk lines ("Kal Kal Kal Googie Hacking D"). Ingesting that
would add noise chunks that can win retrieval for the wrong reasons.

HOW: a line is kept only if most of its words are words the course itself
actually says. The vocabulary comes from the matching TRANSCRIPTS (words of
3+ letters that appear at least MIN_WORD_FREQUENCY times - a word said once
is more likely a mis-heard one than vocabulary). Garbage OCR tokens are
almost never in that set; real slide text and commands ("nmap", "firewall")
almost always are.

NON-DESTRUCTIVE: the raw <stem>.screen.txt is never modified; the filtered
result is written as <stem>.screen.clean.txt, and the header of the clean file
says what was filtered and how strongly. Timestamps are kept only for
blocks that still have text after filtering.

Usage:
    python scripts/clean_screen_text.py <course_output_dir> [--min-ratio 0.6] [--show-samples]
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

WORD_RE = re.compile(r"[A-Za-z]{3,}")
TIMESTAMP_RE = re.compile(r"^\[\d\d:\d\d:\d\d\]$")
# Not calibrated: a word said once in ~100 hours of speech is treated as noise.
MIN_WORD_FREQUENCY = 2
# A line needs at least this many 3+ letter words to be judged at all...
MIN_WORDS_PER_LINE = 2
# ...except a line with digits/operators, where ONE course word is enough:
# commands ("ping 8.8.8.8"), address ranges and formulas would otherwise be
# dropped for having a single word. Found by reading what the first version
# threw away; pure-symbol formula lines with no word at all are still lost
# (the raw .screen.txt keeps them).
TECHNICAL_CHARS_RE = re.compile(r"[0-9=()+\-/*<>^]")


def build_vocabulary(course_dir: Path) -> set[str]:
    counts: Counter[str] = Counter()
    for transcript in course_dir.glob("*.txt"):
        if transcript.name.endswith((".screen.txt", ".screen.clean.txt")):
            continue
        counts.update(w.lower() for w in WORD_RE.findall(transcript.read_text(encoding="utf-8")))
    return {w for w, n in counts.items() if n >= MIN_WORD_FREQUENCY}


def line_ok(line: str, vocab: set[str], min_ratio: float) -> bool:
    words = [w.lower() for w in WORD_RE.findall(line)]
    minimum = 1 if TECHNICAL_CHARS_RE.search(line) else MIN_WORDS_PER_LINE
    if len(words) < minimum:
        return False
    return sum(w in vocab for w in words) / len(words) >= min_ratio


def clean_file(path: Path, vocab: set[str], min_ratio: float, samples: list) -> tuple[int, int]:
    raw = path.read_text(encoding="utf-8").splitlines()
    header, body = raw[:3], raw[3:]

    blocks: list[tuple[str, list[str]]] = []
    current_ts, current = "", []
    for line in body:
        if TIMESTAMP_RE.match(line.strip()):
            if current_ts:
                blocks.append((current_ts, current))
            current_ts, current = line.strip(), []
        elif line.strip():
            current.append(line)
    if current_ts:
        blocks.append((current_ts, current))

    kept_out: list[str] = []
    total = kept = 0
    for ts, lines in blocks:
        good = []
        for ln in lines:
            total += 1
            if line_ok(ln, vocab, min_ratio):
                good.append(ln)
                kept += 1
            elif len(samples) < 400:
                samples.append(("DROP", ln))
        if good:
            kept_out += ["", ts] + good

    out = path.with_name(path.name.replace(".screen.txt", ".screen.clean.txt"))
    note = (
        f"Filtered: kept only lines whose words mostly occur in this course's own transcripts "
        f"(min word ratio {min_ratio}); {kept} of {total} OCR lines kept. Raw OCR is in the matching .screen.txt."
    )
    out.write_text("\n".join(header[:2] + [note, ""] + kept_out) + "\n", encoding="utf-8")
    return kept, total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("course_dir", type=Path)
    parser.add_argument("--min-ratio", type=float, default=0.6)
    parser.add_argument("--show-samples", action="store_true")
    args = parser.parse_args()

    vocab = build_vocabulary(args.course_dir)
    print(f"Vocabulary from transcripts: {len(vocab):,} words")
    samples: list = []
    grand_kept = grand_total = 0
    for path in sorted(args.course_dir.glob("*.screen.txt")):
        kept, total = clean_file(path, vocab, args.min_ratio, samples)
        grand_kept += kept
        grand_total += total
        pct = 100 * kept / total if total else 0
        print(f"  {path.name:34s} kept {kept:>6,} / {total:>6,} lines ({pct:4.0f}%)")
    print(f"TOTAL kept {grand_kept:,} / {grand_total:,} ({100 * grand_kept / max(grand_total, 1):.0f}%)")

    if args.show_samples:
        import random

        random.seed(3)
        print("\nSample DROPPED lines:")
        for _, ln in random.sample(samples, min(14, len(samples))):
            print("   x", ln[:90])


if __name__ == "__main__":
    main()

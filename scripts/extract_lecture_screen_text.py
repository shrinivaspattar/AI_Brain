#!/usr/bin/env python3
"""Extracts the on-screen text of a recorded lecture (OBS screen recording of
a course player) to a plain-text file with timestamps, using Tesseract OCR.
Companion to transcribe_video_lecture.py: the transcript holds what was SAID,
this holds what was SHOWN (slide definitions, notation, the course-outline
sidebar) - measured on a real recording, the slide's formal definitions
(N = {1,2,3,...}) were not in the spoken transcript at all.

WHAT IT DOES NOT DO, honestly: Tesseract cannot read handwriting, and it
garbles some maths symbols (braces/parentheses). Words below a confidence
floor are dropped rather than emitted as garbage. The bottom caption band is
cropped out because captions just duplicate the transcript. The output file
says so in its header so a retrieved chunk is never mistaken for verified
text.

HOW (memory-safe by construction, same lesson as the transcriber): the video
is processed in WINDOW_SECONDS windows; for each, ffmpeg decodes ONLY
keyframes (`-skip_frame nokey`, ~4 s apart in OBS output - 80 frames from a
20-minute window in ~4.5 s) at one frame per INTERVAL_SECONDS into a temp
directory, each frame is OCR'd, and the frames are deleted. Lines already
seen in the last few frames are not repeated, so a static slide is emitted
once, not every 15 seconds.

RESUMABLE like the transcriber: text is appended per window, progress is
saved next to the output (.partial/.progress), and the file only becomes
<stem>.screen.txt when the whole recording is done.

Usage:
    python scripts/extract_lecture_screen_text.py <video> [<video> ...] \\
        --out-dir documents/udemy_transcripts/<course-name>
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path

# Tesseract otherwise spawns ~3 threads per frame; one at a time keeps this a
# polite background job on a shared 8-core machine.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

import pytesseract  # noqa: E402
from PIL import Image, ImageOps, ImageStat  # noqa: E402

WINDOW_SECONDS = 1200
# Not tuned: 15 s catches a slide change within one sentence of speech while
# keeping a 9-hour recording to ~2,200 frames.
INTERVAL_SECONDS = 15
# Caption band (Udemy burns captions into the bottom ~12% of the frame).
CROP_BOTTOM_FRACTION = 0.12
# A word below this Tesseract confidence (0-100) is dropped as noise; picked
# from the observed behaviour on handwriting/symbols, not a calibrated value.
MIN_WORD_CONFIDENCE = 60
MIN_LINE_ALNUM_CHARS = 4
# How many previous frames' lines suppress a repeat.
DEDUP_FRAMES = 4
# Recordings often run for hours on an unchanged screen (idle, paused, a quiz
# page); a frame whose 64x36 grayscale thumbnail differs from the last OCR'd
# frame by less than this mean absolute pixel difference (0-255 scale) is
# skipped without OCR. Not calibrated - a slide change moves this by tens.
UNCHANGED_FRAME_DIFF = 1.5
THUMB_SIZE = (64, 36)
TIMESTAMP_FORMAT = "[{h:02d}:{m:02d}:{s:02d}]"


def _format_ts(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return TIMESTAMP_FORMAT.format(h=h, m=m, s=s)


def _duration_seconds(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _extract_frames(video: Path, start: float, length: float, out_dir: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-skip_frame", "nokey", "-ss", str(start), "-t", str(length),
            "-i", str(video), "-vf", f"fps=1/{INTERVAL_SECONDS}", "-q:v", "3", "-vsync", "vfr",
            str(out_dir / "f_%05d.jpg"),
        ],
        check=True,
    )


def _ocr_lines(image_path: Path) -> list[str]:
    im = Image.open(image_path).convert("L")
    w, h = im.size
    im = im.crop((0, 0, w, int(h * (1 - CROP_BOTTOM_FRACTION))))
    stat = ImageStat.Stat(im)
    if stat.stddev[0] < 3:  # blank frame
        return []
    if stat.mean[0] < 128:  # light-on-dark slide: invert for Tesseract
        im = ImageOps.invert(im)
    im = ImageOps.autocontrast(im, cutoff=2)

    data = pytesseract.image_to_data(im, config="--psm 6", output_type=pytesseract.Output.DICT)
    lines: dict[tuple[int, int, int], list[str]] = {}
    for i, word in enumerate(data["text"]):
        word = word.strip()
        try:
            conf = float(data["conf"][i])
        except ValueError:
            continue
        if not word or conf < MIN_WORD_CONFIDENCE or not any(c.isalnum() for c in word):
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)

    kept = []
    for words in lines.values():
        text = " ".join(words)
        if sum(c.isalnum() for c in text) >= MIN_LINE_ALNUM_CHARS:
            kept.append(text)
    return kept


def _thumbnail(image_path: Path) -> bytes:
    with Image.open(image_path) as im:
        return im.convert("L").resize(THUMB_SIZE).tobytes()


def _unchanged(a: bytes | None, b: bytes) -> bool:
    if a is None:
        return False
    return sum(abs(x - y) for x, y in zip(a, b)) / len(b) < UNCHANGED_FRAME_DIFF


def _normalise(line: str) -> str:
    return "".join(c.lower() for c in line if c.isalnum())


# OCR of the same static slide varies slightly frame to frame, so exact
# matching re-emits it; treat a line as already seen if it is contained in,
# contains, or is >= this similar to a recent line. Not calibrated - chosen
# by eye on one real 10-minute slice.
SIMILARITY_THRESHOLD = 0.8


def _already_seen(norm: str, seen: set[str]) -> bool:
    for other in seen:
        if norm in other or other in norm or SequenceMatcher(None, norm, other).ratio() >= SIMILARITY_THRESHOLD:
            return True
    return False


def extract_to_file(video: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_name(out_path.name + ".partial")
    progress = out_path.with_name(out_path.name + ".progress")

    duration = _duration_seconds(video)
    start = 0.0
    if partial.exists() and progress.exists():
        start = float(progress.read_text().strip())
        print(f"Resuming {video.name} from {_format_ts(start)}", flush=True)
    else:
        partial.write_text(
            f"Source recording: {video}\n"
            "On-screen text recovered by OCR (Tesseract). Handwriting is not recognised and maths "
            "symbols may be garbled; captions are excluded. Spoken content is in the matching transcript.\n\n",
            encoding="utf-8",
        )

    print(f"OCR of {video.name} ({duration / 3600:.2f} h) -> {out_path}", flush=True)
    t0 = time.time()
    recent: list[set[str]] = []
    frames_done = 0
    frames_skipped = 0
    last_thumb: bytes | None = None

    while start < duration:
        with tempfile.TemporaryDirectory(prefix="lecture_frames_") as tmp:
            tmp_dir = Path(tmp)
            _extract_frames(video, start, WINDOW_SECONDS, tmp_dir)
            with partial.open("a", encoding="utf-8") as fh:
                for index, frame in enumerate(sorted(tmp_dir.glob("f_*.jpg"))):
                    frame_time = start + index * INTERVAL_SECONDS
                    thumb = _thumbnail(frame)
                    if _unchanged(last_thumb, thumb):
                        frames_skipped += 1
                        frames_done += 1
                        continue
                    last_thumb = thumb
                    lines = _ocr_lines(frame)
                    seen = set().union(*recent) if recent else set()
                    fresh = []
                    for ln in lines:
                        norm = _normalise(ln)
                        if norm and not _already_seen(norm, seen) and not _already_seen(norm, {_normalise(f) for f in fresh}):
                            fresh.append(ln)
                    recent.append({_normalise(ln) for ln in lines})
                    recent = recent[-DEDUP_FRAMES:]
                    if fresh:
                        fh.write(f"\n{_format_ts(frame_time)}\n" + "\n".join(fresh) + "\n")
                    frames_done += 1

        start += WINDOW_SECONDS
        progress.write_text(str(min(start, duration)))
        print(
            f"  ...{_format_ts(min(start, duration))} of {_format_ts(duration)} done "
            f"({frames_done} frames, {frames_skipped} unchanged/skipped, {(time.time() - t0) / 60:.1f} min elapsed)",
            flush=True,
        )

    partial.rename(out_path)
    progress.unlink(missing_ok=True)
    print(f"  done in {(time.time() - t0) / 60:.1f} min", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("videos", nargs="+", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    for video in args.videos:
        if not video.is_file():
            print(f"Skipping missing file: {video}")
            continue
        out_path = args.out_dir / (video.stem + ".screen.txt")
        if out_path.exists():
            print(f"Skipping (already done): {out_path}")
            continue
        extract_to_file(video, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Transcribes a recorded video lecture (OBS screen recording, Udemy course,
etc.) to a plain-text file with periodic timestamp markers, using the same
faster-whisper setup already built for chat voice input
(app/services/transcription_service.py) - standalone rather than reusing
that service since a multi-hour lecture needs windowing, live progress and
resume, none of which the one-shot chat path needs.

WHY WINDOWS: handing faster-whisper a whole multi-hour file makes it build
features for the entire recording at once - a 9-hour OBS recording was
OOM-killed by the kernel at ~17.7 GB. Each window (WINDOW_SECONDS) is
instead streamed from ffmpeg as raw 16 kHz mono PCM and transcribed
separately, so memory stays small regardless of file length. A word cut at
a window boundary may be slightly garbled; that is the accepted cost.

RESUMABLE: text is appended per window and the finished offset is saved
next to the output (<name>.txt.partial + <name>.txt.progress). Re-running
after a kill/reboot continues from the last finished window. Only when the
whole file is done is the .partial renamed to .txt, so an incomplete
transcript is never mistaken for a finished one.

The output is deliberately plain .txt: AI_Brain's existing ingestion
pipeline already reads .txt as-is - a transcript is just another personal
text file. This script only produces that file; ingesting it is a separate
step.

Usage:
    python scripts/transcribe_video_lecture.py <video_path> [<video_path> ...] \\
        --out-dir documents/udemy_transcripts/<course-name>
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from faster_whisper import WhisperModel  # noqa: E402

# A plain default, not benchmarked against "small"/"tiny" for lecture audio
# specifically - matches STT_MODEL_SIZE's own default in app/core/config.py.
MODEL_SIZE = "base"
# 20 minutes = ~77 MB of float32 audio per window; not tuned, just small
# enough to be safe and large enough that boundary cuts are rare.
WINDOW_SECONDS = 1200
SAMPLE_RATE = 16000
# Insert a "[HH:MM:SS]" marker whenever elapsed time crosses this many
# seconds since the last one.
TIMESTAMP_INTERVAL_SECONDS = 300


def _format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _duration_seconds(video_path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def _read_window(video_path: Path, start: float, length: float) -> np.ndarray:
    """One window of audio as float32 mono 16 kHz, streamed from ffmpeg."""
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", str(start), "-t", str(length), "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
        ],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe_to_file(model: WhisperModel, video_path: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = out_path.with_name(out_path.name + ".partial")
    progress_path = out_path.with_name(out_path.name + ".progress")

    duration = _duration_seconds(video_path)
    resume_from = 0.0
    if partial_path.exists() and progress_path.exists():
        resume_from = float(progress_path.read_text().strip())
        print(f"Resuming {video_path.name} from {_format_timestamp(resume_from)}")
    else:
        partial_path.write_text(f"Source recording: {video_path}\n\n", encoding="utf-8")

    print(f"Transcribing {video_path.name} ({duration/3600:.2f} h) -> {out_path}", flush=True)
    t0 = time.time()
    last_marker = resume_from - TIMESTAMP_INTERVAL_SECONDS
    start = resume_from

    while start < duration:
        audio = _read_window(video_path, start, WINDOW_SECONDS)
        if audio.size:
            segments, _info = model.transcribe(
                audio,
                beam_size=5,
                language="en",
                vad_filter=True,
                condition_on_previous_text=False,
            )
            with partial_path.open("a", encoding="utf-8") as fh:
                for segment in segments:
                    absolute_start = start + segment.start
                    if absolute_start - last_marker >= TIMESTAMP_INTERVAL_SECONDS:
                        fh.write(f"\n[{_format_timestamp(absolute_start)}]\n")
                        last_marker = absolute_start
                    fh.write(segment.text.strip() + " ")

        start += WINDOW_SECONDS
        progress_path.write_text(str(min(start, duration)))
        elapsed = time.time() - t0
        print(
            f"  ...{_format_timestamp(min(start, duration))} of {_format_timestamp(duration)} done, "
            f"{elapsed/60:.1f} min elapsed",
            flush=True,
        )

    partial_path.rename(out_path)
    progress_path.unlink(missing_ok=True)
    print(f"  done in {(time.time() - t0)/60:.1f} min", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("videos", nargs="+", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--model-size", default=MODEL_SIZE)
    args = parser.parse_args(argv)

    print(f"Loading faster-whisper model '{args.model_size}' (cpu/int8)...", flush=True)
    model = WhisperModel(args.model_size, device="cpu", compute_type="int8")

    for video_path in args.videos:
        if not video_path.is_file():
            print(f"Skipping missing file: {video_path}")
            continue
        out_path = args.out_dir / (video_path.stem + ".txt")
        if out_path.exists():
            print(f"Skipping (already transcribed): {out_path}")
            continue
        transcribe_to_file(model, video_path, out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

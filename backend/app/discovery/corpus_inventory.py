"""Read-only corpus discovery/inventory - the T7 Corpus Discovery
milestone. This module NEVER writes, moves, renames, deletes, extracts,
hashes file content, or otherwise mutates anything it scans - it is a
plain, resilient directory walk that reads only filesystem METADATA
(`os.walk`/`os.lstat` results and path names/extensions), never a
file's actual bytes.

Deliberately NOT wired into `SourceScanner`, `DocumentIngestor`, or any
other part of AI_Brain's existing ingestion/indexing pipeline, and does
not touch the database at all - this is a standalone, separate track
from both filesystem mutation (the dedup executor) and document
ingestion, matching the two-track separation this project has kept
throughout: read-only corpus understanding vs. authorized mutation.
"""

from __future__ import annotations

import heapq
import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Extensions app.ingestion.document_ingestor.ARCHIVE_CONTAINER_SUFFIXES
# already recognizes and can act on downstream, kept in sync manually
# (not imported) since this module must stay independent of the
# ingestion pipeline - duplicating one small constant here is far
# cheaper than coupling a read-only discovery tool to a mutation-
# adjacent module.
KNOWN_ARCHIVE_SUFFIXES = frozenset({".zip", ".7z"})

# Every other archive-shaped extension this personal backup corpus
# might plausibly contain, which AI_Brain cannot yet extract - the
# inventory reports both sets so a human can see the gap between "what
# is actually on the drive" and "what this project can currently act
# on." Extension-based only (see module docstring and `scan_corpus`):
# no archive is opened or signature-verified in this first pass.
OTHER_ARCHIVE_SUFFIXES = frozenset(
    {
        ".rar", ".tar", ".gz", ".tgz", ".tar.gz", ".bz2", ".tbz2",
        ".tar.bz2", ".xz", ".txz", ".tar.xz", ".iso", ".cab", ".arj",
        ".lz", ".lzma", ".z", ".zst", ".tzst",
    }
)

ARCHIVE_SUFFIXES = KNOWN_ARCHIVE_SUFFIXES | OTHER_ARCHIVE_SUFFIXES

_DEFAULT_TOP_N = 20


@dataclass(frozen=True, slots=True)
class ScanError:
    """One filesystem entry (or directory listing) this scan could not
    read - recorded and skipped, never raised, so a single permission
    problem on a 727GB foreign drive can't abort the whole inventory."""

    path: str
    message: str


@dataclass(frozen=True, slots=True)
class SizedPath:
    """One path and its size in bytes - used for both the largest-files
    and largest-directories rankings."""

    path: str
    size_bytes: int


@dataclass(slots=True)
class CorpusInventory:
    """Aggregate, read-only statistics for one scanned root. Every
    field here is derived purely from filesystem metadata - no file's
    content is ever read, and nothing here can be used to reconstruct
    what any specific file actually contains."""

    root: str
    scanned_at: str
    total_files: int = 0
    total_directories: int = 0
    total_size_bytes: int = 0
    extension_counts: Counter[str] = field(default_factory=Counter)
    extension_size_bytes: Counter[str] = field(default_factory=Counter)
    archive_counts: Counter[str] = field(default_factory=Counter)
    archive_total_size_bytes: int = 0
    known_archive_total: int = 0
    other_archive_total: int = 0
    largest_files: list[SizedPath] = field(default_factory=list)
    largest_directories: list[SizedPath] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        """A plain, JSON-serializable dict - Counter -> dict, dataclass
        list items -> plain dicts, largest-* rankings sorted descending
        (the internal heaps are min-heaps, ascending by construction)."""
        return {
            "root": self.root,
            "scanned_at": self.scanned_at,
            "total_files": self.total_files,
            "total_directories": self.total_directories,
            "total_size_bytes": self.total_size_bytes,
            "extension_counts": dict(
                sorted(self.extension_counts.items(), key=lambda kv: -kv[1])
            ),
            "extension_size_bytes": dict(
                sorted(self.extension_size_bytes.items(), key=lambda kv: -kv[1])
            ),
            "archive_counts": dict(
                sorted(self.archive_counts.items(), key=lambda kv: -kv[1])
            ),
            "archive_total_size_bytes": self.archive_total_size_bytes,
            "known_archive_total": self.known_archive_total,
            "other_archive_total": self.other_archive_total,
            "largest_files": [
                asdict(p) for p in sorted(self.largest_files, key=lambda p: -p.size_bytes)
            ],
            "largest_directories": [
                asdict(p)
                for p in sorted(self.largest_directories, key=lambda p: -p.size_bytes)
            ],
            "error_count": len(self.errors),
            "errors": [asdict(e) for e in self.errors],
        }


class _TopNTracker:
    """Bounded min-heap keeping the N largest (size, path) pairs seen
    so far, without ever holding the full path list in memory - safe
    for a corpus with an unknown, potentially very large file count."""

    def __init__(self, top_n: int):
        self._top_n = top_n
        self._heap: list[tuple[int, str]] = []

    def offer(self, path: str, size_bytes: int) -> None:
        if self._top_n <= 0:
            return
        if len(self._heap) < self._top_n:
            heapq.heappush(self._heap, (size_bytes, path))
        elif size_bytes > self._heap[0][0]:
            heapq.heapreplace(self._heap, (size_bytes, path))

    def to_list(self) -> list[SizedPath]:
        return [SizedPath(path=p, size_bytes=s) for s, p in self._heap]


def scan_corpus(root: Path, *, top_n: int = _DEFAULT_TOP_N) -> CorpusInventory:
    """Walk `root` read-only and build an aggregate `CorpusInventory`.

    Never mutates anything: every filesystem call here is `os.walk`
    (directory listing) or `os.lstat` (metadata for one entry, and
    `lstat` specifically - never following a symlink - so a symlink
    itself is measured, not whatever it points to, and a symlink loop
    can never cause unbounded traversal). No file is ever opened for
    its content, hashed, extracted, or otherwise read beyond its
    directory-entry metadata.

    Resilient by design: a directory that can't be listed, or a file
    entry whose `lstat` fails (permission denied, a broken/dangling
    entry, a device disconnecting mid-scan), is recorded in the
    returned inventory's `errors` list and skipped - never raised,
    since aborting an entire multi-hundred-gigabyte scan over one
    unreadable entry would defeat the point of a resilient inventory.

    Directory sizes are computed bottom-up in a single pass using
    `os.walk(topdown=False)`, which yields each directory only after
    all of its subdirectories - so by the time a directory is
    processed, every child directory's own total is already known and
    can simply be summed in, with no second pass over the tree needed.

    `top_n` bounds memory for the two rankings (largest files, largest
    directories) via a min-heap per ranking - the full corpus is never
    held in memory as a per-file list, only these bounded rankings plus
    the aggregate counters.
    """
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    inventory = CorpusInventory(
        root=str(root), scanned_at=datetime.now(UTC).isoformat()
    )
    largest_files = _TopNTracker(top_n)
    largest_directories = _TopNTracker(top_n)

    # Populated bottom-up as os.walk yields each directory (topdown=
    # False guarantees every subdirectory of `dirpath` was already
    # yielded, and therefore already has an entry here, before
    # `dirpath` itself is processed).
    directory_totals: dict[str, int] = {}

    def _on_walk_error(exc: OSError) -> None:
        inventory.errors.append(
            ScanError(path=getattr(exc, "filename", None) or str(root), message=str(exc))
        )

    for dirpath, dirnames, filenames in os.walk(
        root, topdown=False, onerror=_on_walk_error, followlinks=False
    ):
        inventory.total_directories += 1
        own_total = 0

        for filename in filenames:
            file_path = os.path.join(dirpath, filename)
            try:
                stat_result = os.lstat(file_path)
            except OSError as exc:
                inventory.errors.append(ScanError(path=file_path, message=str(exc)))
                continue

            size = stat_result.st_size
            inventory.total_files += 1
            inventory.total_size_bytes += size
            own_total += size

            suffix = _classify_extension(filename)
            inventory.extension_counts[suffix] += 1
            inventory.extension_size_bytes[suffix] += size

            if suffix in ARCHIVE_SUFFIXES:
                inventory.archive_counts[suffix] += 1
                inventory.archive_total_size_bytes += size
                if suffix in KNOWN_ARCHIVE_SUFFIXES:
                    inventory.known_archive_total += 1
                else:
                    inventory.other_archive_total += 1

            largest_files.offer(file_path, size)

        subtree_total = own_total + sum(
            directory_totals.get(os.path.join(dirpath, d), 0) for d in dirnames
        )
        directory_totals[dirpath] = subtree_total
        largest_directories.offer(dirpath, subtree_total)

    inventory.largest_files = largest_files.to_list()
    inventory.largest_directories = largest_directories.to_list()
    return inventory


def _classify_extension(filename: str) -> str:
    """Lowercased suffix, with `.tar.gz`/`.tar.bz2`/`.tar.xz`/`.tar.zst`
    recognized as their own two-part extensions rather than being
    classified merely as `.gz`/`.bz2`/`.xz`/`.zst` - a materially
    different, and much more common, archive shape in a personal
    backup corpus. Files with no extension are grouped under a single
    explicit label rather than an empty string, so they show up as
    their own named category in a report rather than looking like a
    parsing gap."""
    name = filename.lower()
    for two_part in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if name.endswith(two_part):
            return two_part
    suffix = Path(filename).suffix.lower()
    return suffix if suffix else "(no extension)"


def write_inventory_report(inventory: CorpusInventory, destination: Path) -> Path:
    """Write `inventory` as pretty-printed JSON to `destination`
    (creating parent directories as needed) and return the path
    written.

    Fail-closed, structurally rather than by caller convention: refuses
    (`ValueError`, no directory created, nothing written) if
    `destination` resolves to the scanned root itself or anywhere
    inside it. The whole point of this milestone is that discovery
    never mutates the corpus it describes - a report written back into
    that same corpus would be exactly that, regardless of how
    well-behaved every CURRENT caller happens to be. Checked via
    `.resolve(strict=False)` on BOTH `inventory.root` and `destination`
    - never the raw, unresolved strings - so a `..` segment or a
    symlinked intermediate directory that resolves into the root is
    caught, not merely a literal path-string prefix match.
    `strict=False` is deliberate: `destination` (and possibly several
    of its trailing components) need not exist yet - whatever prefix
    already exists on disk is resolved through any symlinks, and the
    rest is appended literally, which is exactly the containment
    question that matters here.
    """
    destination = Path(destination)
    scanned_root = Path(inventory.root).resolve(strict=False)
    resolved_destination = destination.resolve(strict=False)

    if resolved_destination == scanned_root or resolved_destination.is_relative_to(
        scanned_root
    ):
        raise ValueError(
            f"Report destination {destination} resolves to "
            f"{resolved_destination}, which is the scanned root "
            f"{scanned_root} or a location inside it - a discovery "
            "report must never be written into the corpus it describes"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(inventory.to_json_dict(), indent=2, default=str))
    return destination

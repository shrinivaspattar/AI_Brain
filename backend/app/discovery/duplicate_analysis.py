"""T7 Deduplication Analysis (Phase D1) - read-only, exactly as
authorized: reading file metadata and content for hashing, reading
directory structure, comparing files/directories, and producing an
analysis report. This module NEVER deletes, moves, renames, extracts,
quarantines, or otherwise modifies anything it scans, and NEVER
performs deduplication execution of any kind - it only observes and
reports.

The one meaningful difference from `corpus_inventory.py`: this module
DOES read file *content* (to compute a SHA-256 hash), not just
metadata - that is exactly what "computing hashes for duplicate
detection" was authorized to mean. Every read is a plain, buffered,
read-only open (`"rb"` mode) - never `"r+"`/`"w"`/`"a"`, and no file is
ever written to, truncated, or extracted.

Deliberately independent of `app.dedup.*` (the database-backed,
mutation-capable dedup executor pipeline) and of
`app.ingestion.document_ingestor` - this operates directly on the raw
filesystem, produces no `Document` rows, and touches no database at
all. `app.discovery.corpus_inventory`'s aggregate-only `inventory.json`
does not retain a full per-file listing (only the top-N largest files/
directories - see that module's `_TopNTracker`), so genuine duplicate
detection requires its own full-corpus pass; this module does not
re-scan using `corpus_inventory.scan_corpus` and instead performs one
combined metadata pass of its own (size index + directory structural
signatures), followed by a second, separate pass that reads content
only for files inside a real size collision.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.discovery.safety import reject_destination_inside_root

_HASH_READ_CHUNK_SIZE = 1024 * 1024

# Cryptomator's encrypted-chunk file extension. Two identical .c9r
# files are ciphertext-identical, not necessarily meaningful "the same
# user document appears twice" duplicates in the way a repeated .jpg
# or .zip is - reported as their own separate, explicitly-labeled
# category rather than being folded into ordinary duplicate groups.
CRYPTOMATOR_CHUNK_SUFFIX = ".c9r"


@dataclass(frozen=True, slots=True)
class ScanError:
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """One set of 2+ files confirmed to have identical size AND
    identical SHA-256 content hash - a genuine exact-duplicate finding,
    not merely a same-size candidate."""

    size_bytes: int
    content_hash: str
    paths: list[str]
    copies: int
    reclaimable_bytes: int
    category: str  # "exact_duplicate" or "cryptomator_chunk_duplicate"


@dataclass(frozen=True, slots=True)
class DirectoryDuplicateGroup:
    """2+ directories whose entire subtree matches by name and size,
    recursively - a STRUCTURAL signal (same names, same sizes,
    everywhere in the tree), not independently content-hash-verified
    for every file within. Cross-reference with `exact_duplicate_
    groups` for per-file content confirmation of any specific file
    inside one of these directories."""

    signature: str
    paths: list[str]
    total_size_bytes: int
    file_count: int
    reclaimable_bytes: int


@dataclass(slots=True)
class DuplicateAnalysis:
    """`exact_duplicate_reclaimable_bytes` and `directory_duplicate_
    reclaimable_bytes` are two DIFFERENT LENSES over OVERLAPPING data,
    not two independent categories of unique bytes - a file inside a
    duplicated directory tree is very likely ALSO counted in the
    file-level exact-duplicate total (since a whole-tree structural
    match implies most or all of its individual files also collide by
    size and hash). **Adding these two totals together overstates
    genuinely reclaimable space, likely substantially** - never sum
    them; report them as two separate, corroborating pieces of
    evidence about the same underlying duplication instead."""

    root: str
    analyzed_at: str
    total_files_considered: int = 0
    total_directories_considered: int = 0
    size_collision_candidate_files: int = 0
    size_collision_candidate_bytes: int = 0
    files_hashed: int = 0
    exact_duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    cryptomator_chunk_duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    directory_duplicate_groups: list[DirectoryDuplicateGroup] = field(default_factory=list)
    exact_duplicate_reclaimable_bytes: int = 0
    directory_duplicate_reclaimable_bytes: int = 0
    errors: list[ScanError] = field(default_factory=list)
    hash_errors: list[ScanError] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "root": self.root,
            "analyzed_at": self.analyzed_at,
            "total_files_considered": self.total_files_considered,
            "total_directories_considered": self.total_directories_considered,
            "size_collision_candidate_files": self.size_collision_candidate_files,
            "size_collision_candidate_bytes": self.size_collision_candidate_bytes,
            "files_hashed": self.files_hashed,
            "exact_duplicate_group_count": len(self.exact_duplicate_groups),
            "exact_duplicate_reclaimable_bytes": self.exact_duplicate_reclaimable_bytes,
            "exact_duplicate_groups": [
                asdict(g)
                for g in sorted(
                    self.exact_duplicate_groups, key=lambda g: -g.reclaimable_bytes
                )
            ],
            "cryptomator_chunk_duplicate_group_count": len(
                self.cryptomator_chunk_duplicate_groups
            ),
            "cryptomator_chunk_duplicate_groups": [
                asdict(g)
                for g in sorted(
                    self.cryptomator_chunk_duplicate_groups,
                    key=lambda g: -g.reclaimable_bytes,
                )
            ],
            "directory_duplicate_group_count": len(self.directory_duplicate_groups),
            "directory_duplicate_reclaimable_bytes": self.directory_duplicate_reclaimable_bytes,
            "directory_duplicate_groups": [
                asdict(g)
                for g in sorted(
                    self.directory_duplicate_groups, key=lambda g: -g.reclaimable_bytes
                )
            ],
            "error_count": len(self.errors),
            "errors": [asdict(e) for e in self.errors],
            "hash_error_count": len(self.hash_errors),
            "hash_errors": [asdict(e) for e in self.hash_errors],
        }


def _hash_file(path: str) -> str | None:
    """SHA-256 of a file's content, streamed in fixed-size chunks -
    read-only (`"rb"`), never loading the whole file into memory.
    Returns `None` (rather than raising) if the file can't be read -
    the caller records this as a hash error and moves on, exactly like
    `corpus_inventory.scan_corpus` never aborts over one bad entry."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(_HASH_READ_CHUNK_SIZE), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _compute_directory_signature(
    own_files: list[tuple[str, int]], child_signatures: list[tuple[str, str]]
) -> str:
    """A SHA-256 digest deterministically derived from this directory's
    own (name, size) file pairs plus its immediate subdirectories'
    already-computed signatures. Two directories with the same
    signature have IDENTICAL names and sizes recursively throughout
    their entire subtree - a structural equality claim, not a
    byte-content one (see `DirectoryDuplicateGroup`'s own docstring)."""
    hasher = hashlib.sha256()
    for name, size in own_files:
        hasher.update(f"F:{name}:{size}\n".encode())
    for name, sig in child_signatures:
        hasher.update(f"D:{name}:{sig}\n".encode())
    return hasher.hexdigest()


def _find_exact_duplicate_groups(
    size_index: dict[int, list[str]],
) -> tuple[list[DuplicateGroup], list[DuplicateGroup], list[ScanError], int]:
    """Hash every file inside a real size collision (2+ files sharing a
    size) and group by (size, hash) - only a group that survives
    hashing with 2+ members is a confirmed exact duplicate. Returns
    (regular_groups, cryptomator_chunk_groups, hash_errors, files_hashed)."""
    regular_groups: list[DuplicateGroup] = []
    crypto_groups: list[DuplicateGroup] = []
    hash_errors: list[ScanError] = []
    files_hashed = 0

    for size, paths in size_index.items():
        if size == 0 or len(paths) < 2:
            # A zero-byte "duplicate" reclaims nothing - not a
            # meaningful finding, and would otherwise dump every
            # zero-byte file in the corpus into one giant group.
            continue

        by_hash: dict[str, list[str]] = defaultdict(list)
        for path in paths:
            digest = _hash_file(path)
            files_hashed += 1
            if digest is None:
                hash_errors.append(ScanError(path=path, message="could not be read for hashing"))
                continue
            by_hash[digest].append(path)

        for digest, group_paths in by_hash.items():
            if len(group_paths) < 2:
                continue
            group = DuplicateGroup(
                size_bytes=size,
                content_hash=digest,
                paths=sorted(group_paths),
                copies=len(group_paths),
                reclaimable_bytes=size * (len(group_paths) - 1),
                category=(
                    "cryptomator_chunk_duplicate"
                    if all(p.lower().endswith(CRYPTOMATOR_CHUNK_SUFFIX) for p in group_paths)
                    else "exact_duplicate"
                ),
            )
            if group.category == "cryptomator_chunk_duplicate":
                crypto_groups.append(group)
            else:
                regular_groups.append(group)

    return regular_groups, crypto_groups, hash_errors, files_hashed


def _find_directory_duplicate_groups(
    signatures: dict[str, str], totals: dict[str, tuple[int, int]]
) -> list[DirectoryDuplicateGroup]:
    """Group directories by signature, keeping only groups of 2+
    non-empty directories, and reporting only the TOPMOST match in any
    nested chain: if a directory's own parent is also part of a
    duplicate-directory group, this directory's match is fully implied
    by (and would be redundant noise alongside) that parent-level
    match, so it is omitted here - it still contributes to the
    parent's own `total_size_bytes` via the bottom-up total."""
    by_signature: dict[str, list[str]] = defaultdict(list)
    for path, sig in signatures.items():
        by_signature[sig].append(path)

    duplicate_paths = {
        p for paths in by_signature.values() if len(paths) >= 2 for p in paths
    }

    groups: list[DirectoryDuplicateGroup] = []
    for sig, paths in by_signature.items():
        if len(paths) < 2:
            continue
        total_size, total_files = totals[paths[0]]
        if total_files == 0:
            continue  # empty-directory noise - not a meaningful finding
        if all(os.path.dirname(p) in duplicate_paths for p in paths):
            continue  # fully explained by an already-reported parent match

        groups.append(
            DirectoryDuplicateGroup(
                signature=sig,
                paths=sorted(paths),
                total_size_bytes=total_size,
                file_count=total_files,
                reclaimable_bytes=total_size * (len(paths) - 1),
            )
        )

    return groups


def analyze_duplicates(root: Path) -> DuplicateAnalysis:
    """Perform the full Phase D1 read-only analysis against `root`.

    Pass 1 (metadata only, comparable cost to `corpus_inventory.
    scan_corpus`): a single `os.walk(topdown=False)` traversal builds a
    full size index (every file's path, grouped by `os.lstat` size -
    `corpus_inventory`'s own report does not retain this, only a
    bounded top-N) and, in the SAME pass, each directory's structural
    signature computed bottom-up.

    Pass 2 (reads file CONTENT, the expensive part): every file inside
    a real size collision - i.e. sharing its size with at least one
    other file - is opened read-only and SHA-256 hashed. Files with no
    size collision are never opened at all, since they cannot possibly
    be an exact duplicate of anything.

    Never mutates anything: `os.walk`, `os.lstat`, and `open(path,
    "rb")` are the only filesystem operations against `root` anywhere
    in this function.

    Memory usage is deliberately NOT bounded the way `corpus_inventory.
    scan_corpus`'s top-N rankings are: the size index holds every
    file's path (grouped by size) and `directory_signatures`/
    `directory_totals` hold one entry per directory, both scaling
    linearly with the corpus's file/directory count. This is an
    accepted, necessary tradeoff, not an oversight - genuine duplicate
    detection cannot be done from a bounded top-N sample; it requires
    seeing every file at least once. Observed practical on a real
    ~628,000-file, ~669GB corpus (peak process memory in the low
    hundreds of MB) - a corpus large enough to make this an actual
    concern would need a different, streaming/external-index design,
    out of scope for this milestone.

    Live-corpus caveat: if `root` is actively being modified while this
    function runs (as the real T7 corpus was, by an independently-
    running Syncthing daemon, during the run this milestone was
    verified against), each file's SIZE (from pass 1) and CONTENT hash
    (from pass 2, which runs strictly after pass 1 completes for the
    ENTIRE tree) are captured at two different, potentially widely
    separated points in real time - not one atomic snapshot. Every
    individual hash comparison is still internally valid (each hash
    reflects a real, complete read of that file's actual bytes at the
    moment it was read), but the overall analysis should be understood
    as "assembled from reads spread across the run's real duration,"
    not "the corpus at one single instant." Directory-level structural
    signatures ARE internally consistent with each other (every
    directory's signature comes from the SAME single metadata pass),
    but are not independently content-hash-verified for every file
    within - see `DirectoryDuplicateGroup`'s own docstring.
    """
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    analysis = DuplicateAnalysis(root=str(root), analyzed_at=datetime.now(UTC).isoformat())

    size_index: dict[int, list[str]] = defaultdict(list)
    directory_signatures: dict[str, str] = {}
    directory_totals: dict[str, tuple[int, int]] = {}

    def _on_walk_error(exc: OSError) -> None:
        analysis.errors.append(
            ScanError(path=getattr(exc, "filename", None) or str(root), message=str(exc))
        )

    for dirpath, dirnames, filenames in os.walk(
        root, topdown=False, onerror=_on_walk_error, followlinks=False
    ):
        analysis.total_directories_considered += 1
        own_files: list[tuple[str, int]] = []
        dir_size = 0
        dir_file_count = 0

        for filename in filenames:
            file_path = os.path.join(dirpath, filename)
            try:
                stat_result = os.lstat(file_path)
            except OSError as exc:
                analysis.errors.append(ScanError(path=file_path, message=str(exc)))
                continue

            size = stat_result.st_size
            analysis.total_files_considered += 1
            own_files.append((filename, size))
            dir_size += size
            dir_file_count += 1

            # A symlink is excluded from hash-based duplicate detection
            # specifically (though still counted above, and still
            # included in its directory's structural signature by its
            # own lstat size, exactly like corpus_inventory.py treats
            # it). Reason: `_hash_file` opens the path in "rb" mode,
            # which unavoidably FOLLOWS a symlink to its target -
            # hashing the TARGET's content while `size` here is the
            # SYMLINK's own (typically tiny) lstat size. If that tiny
            # size happened to collide with an unrelated regular
            # file's size, this module would silently read and hash an
            # arbitrary amount of the symlink's target - content
            # unrelated to, and vastly larger than, the size that
            # triggered the comparison - and any reported group
            # containing that path would carry a `size_bytes` field
            # that does not match what was actually hashed. Excluding
            # symlinks here avoids ever calling `_hash_file` on one.
            if stat.S_ISLNK(stat_result.st_mode):
                continue

            size_index[size].append(file_path)

        child_signatures: list[tuple[str, str]] = []
        for dirname in sorted(dirnames):
            child_path = os.path.join(dirpath, dirname)
            signature = directory_signatures.get(child_path)
            if signature is not None:
                child_signatures.append((dirname, signature))
                child_size, child_files = directory_totals.get(child_path, (0, 0))
                dir_size += child_size
                dir_file_count += child_files

        own_files.sort()
        directory_signatures[dirpath] = _compute_directory_signature(
            own_files, child_signatures
        )
        directory_totals[dirpath] = (dir_size, dir_file_count)

    collision_sizes = {size: paths for size, paths in size_index.items() if len(paths) >= 2}
    analysis.size_collision_candidate_files = sum(len(p) for p in collision_sizes.values())
    analysis.size_collision_candidate_bytes = sum(
        size * len(paths) for size, paths in collision_sizes.items()
    )

    (
        analysis.exact_duplicate_groups,
        analysis.cryptomator_chunk_duplicate_groups,
        analysis.hash_errors,
        analysis.files_hashed,
    ) = _find_exact_duplicate_groups(size_index)

    analysis.directory_duplicate_groups = _find_directory_duplicate_groups(
        directory_signatures, directory_totals
    )

    analysis.exact_duplicate_reclaimable_bytes = sum(
        g.reclaimable_bytes for g in analysis.exact_duplicate_groups
    )
    analysis.directory_duplicate_reclaimable_bytes = sum(
        g.reclaimable_bytes for g in analysis.directory_duplicate_groups
    )

    return analysis


def write_duplicate_report(analysis: DuplicateAnalysis, destination: Path) -> Path:
    """Write `analysis` as pretty-printed JSON to `destination`
    (creating parent directories as needed) and return the path
    written. Refuses (`ValueError`, nothing written) if `destination`
    resolves to the analyzed root or anywhere inside it - see
    `app.discovery.safety.reject_destination_inside_root`."""
    destination = Path(destination)
    reject_destination_inside_root(analysis.root, destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(analysis.to_json_dict(), indent=2, default=str))
    return destination

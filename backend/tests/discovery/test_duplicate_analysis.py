import hashlib
import json
import os
from pathlib import Path

import pytest

from app.discovery.duplicate_analysis import (
    CRYPTOMATOR_CHUNK_SUFFIX,
    analyze_duplicates,
    write_duplicate_report,
)


def test_analyze_rejects_nonexistent_root(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        analyze_duplicates(tmp_path / "does-not-exist")


def test_analyze_rejects_file_root(tmp_path: Path) -> None:
    file_root = tmp_path / "file.txt"
    file_root.write_text("not a directory")

    with pytest.raises(NotADirectoryError):
        analyze_duplicates(file_root)


def test_analyze_counts_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b.txt").write_bytes(b"world")

    analysis = analyze_duplicates(tmp_path)

    assert analysis.total_files_considered == 2
    assert analysis.total_directories_considered == 2  # root + nested


def test_analyze_finds_exact_file_duplicates(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"identical content")
    (tmp_path / "b.txt").write_bytes(b"identical content")
    (tmp_path / "c.txt").write_bytes(b"different content entirely")

    analysis = analyze_duplicates(tmp_path)

    assert len(analysis.exact_duplicate_groups) == 1
    group = analysis.exact_duplicate_groups[0]
    assert group.copies == 2
    assert group.category == "exact_duplicate"
    assert group.size_bytes == len(b"identical content")
    assert group.reclaimable_bytes == len(b"identical content")
    assert group.content_hash == hashlib.sha256(b"identical content").hexdigest()
    assert set(group.paths) == {str(tmp_path / "a.txt"), str(tmp_path / "b.txt")}


def test_analyze_only_hashes_files_with_a_size_collision(tmp_path: Path) -> None:
    """A file with a unique size in the whole corpus can never be an
    exact duplicate of anything - it must never even be opened."""
    (tmp_path / "unique_size.txt").write_bytes(b"x" * 12345)
    (tmp_path / "a.txt").write_bytes(b"y" * 10)
    (tmp_path / "b.txt").write_bytes(b"z" * 10)  # same size as a.txt, different content

    analysis = analyze_duplicates(tmp_path)

    # Only the two same-size files were candidates; the uniquely-sized
    # file was never a hashing candidate at all.
    assert analysis.size_collision_candidate_files == 2
    assert analysis.files_hashed == 2
    assert analysis.exact_duplicate_groups == []  # different content, same size only


def test_analyze_excludes_zero_byte_files_from_duplicate_groups(tmp_path: Path) -> None:
    (tmp_path / "empty1.txt").write_bytes(b"")
    (tmp_path / "empty2.txt").write_bytes(b"")
    (tmp_path / "empty3.txt").write_bytes(b"")

    analysis = analyze_duplicates(tmp_path)

    assert analysis.exact_duplicate_groups == []
    assert analysis.files_hashed == 0  # never even hashed - filtered before hashing


def test_size_collision_candidate_count_includes_zero_byte_files_but_hashed_count_does_not(
    tmp_path: Path,
) -> None:
    """Explicitly demonstrates the exact counting discrepancy between
    `size_collision_candidate_files` and `files_hashed`, the same
    pattern observed in the real T7 run (624,847 candidates vs 581,414
    hashed): `size_collision_candidate_files` counts every file sharing
    its size with at least one other file, with NO exclusion for
    size==0 - while `files_hashed` is incremented only inside
    `_find_exact_duplicate_groups`'s hashing loop, which explicitly
    skips `size == 0` buckets before ever calling `_hash_file`. The gap
    between the two counts is EXACTLY the number of files sitting in a
    zero-byte size collision - never any other cause, since that `size
    == 0` check is the ONLY skip condition in the hashing loop besides
    `len(paths) < 2` (which `size_collision_candidate_files` already
    excludes by construction)."""
    # 3 zero-byte files sharing size 0 - candidates, but never hashed.
    (tmp_path / "empty1.txt").write_bytes(b"")
    (tmp_path / "empty2.txt").write_bytes(b"")
    (tmp_path / "empty3.txt").write_bytes(b"")
    # 2 real files sharing a non-zero size - candidates AND hashed.
    (tmp_path / "a.txt").write_bytes(b"real content")
    (tmp_path / "b.txt").write_bytes(b"real content")
    # A uniquely-sized file - not a candidate at all, not hashed.
    (tmp_path / "unique.txt").write_bytes(b"x" * 999)

    analysis = analyze_duplicates(tmp_path)

    assert analysis.size_collision_candidate_files == 5  # 3 empty + 2 real
    assert analysis.files_hashed == 2  # only the 2 real, non-empty files
    gap = analysis.size_collision_candidate_files - analysis.files_hashed
    assert gap == 3  # exactly the 3 zero-byte files, nothing else
    # And the hashing that DID happen correctly confirms an exact
    # duplicate for the real content - hashing is not skipped for
    # legitimate collisions, only for the size==0 case.
    assert len(analysis.exact_duplicate_groups) == 1
    assert analysis.exact_duplicate_groups[0].copies == 2


def test_analyze_classifies_cryptomator_chunks_separately(tmp_path: Path) -> None:
    vault1 = tmp_path / "vault1"
    vault1.mkdir()
    vault2 = tmp_path / "vault2"
    vault2.mkdir()
    (vault1 / "chunk.c9r").write_bytes(b"encrypted-bytes-here")
    (vault2 / "chunk.c9r").write_bytes(b"encrypted-bytes-here")
    (tmp_path / "regular_a.txt").write_bytes(b"regular duplicate content")
    (tmp_path / "regular_b.txt").write_bytes(b"regular duplicate content")

    analysis = analyze_duplicates(tmp_path)

    assert len(analysis.cryptomator_chunk_duplicate_groups) == 1
    assert analysis.cryptomator_chunk_duplicate_groups[0].category == (
        "cryptomator_chunk_duplicate"
    )
    # Cryptomator chunks must never be mixed into the ordinary group.
    assert len(analysis.exact_duplicate_groups) == 1
    assert analysis.exact_duplicate_groups[0].category == "exact_duplicate"


def test_analyze_does_not_classify_mixed_group_as_cryptomator(tmp_path: Path) -> None:
    """A same-hash group containing even one non-.c9r file must be
    reported as an ordinary exact duplicate, not silently folded into
    the Cryptomator-specific category."""
    (tmp_path / "chunk.c9r").write_bytes(b"same bytes")
    (tmp_path / "regular.bin").write_bytes(b"same bytes")

    analysis = analyze_duplicates(tmp_path)

    assert analysis.cryptomator_chunk_duplicate_groups == []
    assert len(analysis.exact_duplicate_groups) == 1
    assert analysis.exact_duplicate_groups[0].category == "exact_duplicate"


def test_analyze_finds_duplicate_directory_trees(tmp_path: Path) -> None:
    for name in ("copy1", "copy2"):
        d = tmp_path / name
        d.mkdir()
        (d / "x.txt").write_bytes(b"aaa")
        (d / "y.txt").write_bytes(b"bbbbb")

    analysis = analyze_duplicates(tmp_path)

    assert len(analysis.directory_duplicate_groups) == 1
    group = analysis.directory_duplicate_groups[0]
    assert group.file_count == 2
    assert group.total_size_bytes == 3 + 5
    assert group.reclaimable_bytes == 3 + 5
    assert set(group.paths) == {str(tmp_path / "copy1"), str(tmp_path / "copy2")}


def test_analyze_directory_duplicates_require_matching_names_and_sizes(
    tmp_path: Path,
) -> None:
    d1 = tmp_path / "copy1"
    d1.mkdir()
    (d1 / "x.txt").write_bytes(b"aaa")
    d2 = tmp_path / "copy2"
    d2.mkdir()
    (d2 / "x.txt").write_bytes(b"aaaa")  # different SIZE - not a structural match

    analysis = analyze_duplicates(tmp_path)

    assert analysis.directory_duplicate_groups == []


def test_analyze_reports_only_topmost_nested_directory_duplicate(tmp_path: Path) -> None:
    """When an entire tree is duplicated (parent AND child directories
    all match), only the TOPMOST match should be reported - reporting
    every nested sub-match too would just be redundant noise from the
    same underlying duplicate tree."""
    for name in ("tree1", "tree2"):
        d = tmp_path / name
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "inner.txt").write_bytes(b"inner content")
        (d / "top.txt").write_bytes(b"top content")

    analysis = analyze_duplicates(tmp_path)

    reported_paths = {p for g in analysis.directory_duplicate_groups for p in g.paths}
    # Only the top-level tree1/tree2 pair should appear - not
    # tree1/sub and tree2/sub as a SEPARATE second reported group.
    assert reported_paths == {str(tmp_path / "tree1"), str(tmp_path / "tree2")}
    assert len(analysis.directory_duplicate_groups) == 1
    # But the parent-level group's totals include the nested file.
    assert analysis.directory_duplicate_groups[0].file_count == 2


def test_analyze_excludes_empty_directories_from_duplicate_groups(tmp_path: Path) -> None:
    (tmp_path / "empty1").mkdir()
    (tmp_path / "empty2").mkdir()
    (tmp_path / "empty3").mkdir()

    analysis = analyze_duplicates(tmp_path)

    assert analysis.directory_duplicate_groups == []


def test_analyze_records_unreadable_file_as_hash_error_without_aborting(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_bytes(b"same size content")
    (tmp_path / "b.txt").write_bytes(b"same size content")

    real_open = open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith("b.txt"):
            raise PermissionError(f"synthetic permission error for {path}")
        return real_open(path, *args, **kwargs)

    import builtins

    original = builtins.open
    builtins.open = failing_open
    try:
        analysis = analyze_duplicates(tmp_path)
    finally:
        builtins.open = original

    assert analysis.exact_duplicate_groups == []  # b.txt couldn't be confirmed
    assert len(analysis.hash_errors) == 1
    assert "b.txt" in analysis.hash_errors[0].path


def test_analyze_records_unlistable_directory_without_aborting(tmp_path: Path) -> None:
    (tmp_path / "readable.txt").write_bytes(b"x")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "hidden.txt").write_bytes(b"y")

    try:
        blocked.chmod(0o000)
        if os.access(blocked, os.R_OK):
            pytest.skip("running as a user that bypasses directory permissions")

        analysis = analyze_duplicates(tmp_path)

        assert analysis.total_files_considered == 1
        assert any("blocked" in e.path for e in analysis.errors)
    finally:
        blocked.chmod(0o755)


def test_analyze_excludes_symlinks_from_hash_based_duplicate_detection(
    tmp_path: Path,
) -> None:
    """A symlink's `os.lstat` size (the symlink object's own tiny size)
    is fundamentally different from what `_hash_file` would actually
    read if given that same path - opening a symlink path in "rb" mode
    unavoidably follows it to the TARGET's content. If a symlink's tiny
    lstat size happened to collide with an unrelated regular file's
    size, hashing the symlink would silently read and hash an
    arbitrary amount of the TARGET's content while reporting a
    `size_bytes` that reflects only the symlink's own size - a data
    integrity mismatch. Symlinks must therefore never enter the
    hash-based duplicate detection pool at all, though they are still
    counted in `total_files_considered`, exactly like
    `corpus_inventory.py` counts them in its own aggregates without
    ever reading their target's content."""
    target = tmp_path / "unrelated_large_target.bin"
    target.write_bytes(b"SECRET-LARGE-CONTENT" * 1000)
    link = tmp_path / "link"
    link.symlink_to(target)
    link_size = os.lstat(link).st_size

    # A decoy regular file whose size deliberately collides with the
    # SYMLINK's own tiny lstat size - the exact scenario that would
    # previously have triggered hashing the symlink (and therefore its
    # target) via `open(path, "rb")`.
    decoy = tmp_path / "decoy.bin"
    decoy.write_bytes(b"Y" * link_size)

    analysis = analyze_duplicates(tmp_path)

    # The symlink is still observed/counted...
    assert analysis.total_files_considered == 3  # target, link, decoy
    # ...but never treated as a hashing candidate, so the decoy - now
    # genuinely unique in size once the symlink is excluded - is never
    # hashed either, and the target's large content is never read via
    # the symlink at all.
    assert analysis.size_collision_candidate_files == 0
    assert analysis.files_hashed == 0
    assert analysis.exact_duplicate_groups == []


def test_analyze_still_counts_symlink_in_directory_structural_signature(
    tmp_path: Path,
) -> None:
    """Excluding symlinks from HASH-based duplicate detection is
    narrowly scoped - they still participate in directory-level
    structural comparison via their own lstat size (metadata only,
    never opened), exactly like a regular file would, so two
    directories that are structurally identical INCLUDING an identical
    symlink are still correctly recognized as matching."""
    target = tmp_path / "shared_target.bin"
    target.write_bytes(b"content")

    for name in ("copy1", "copy2"):
        d = tmp_path / name
        d.mkdir()
        (d / "link").symlink_to(target)

    analysis = analyze_duplicates(tmp_path)

    assert len(analysis.directory_duplicate_groups) == 1
    assert set(analysis.directory_duplicate_groups[0].paths) == {
        str(tmp_path / "copy1"),
        str(tmp_path / "copy2"),
    }


def test_write_duplicate_report_rejects_destination_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    analysis = analyze_duplicates(root)

    with pytest.raises(ValueError, match="scanned root"):
        write_duplicate_report(analysis, root / "report.json")


def test_write_duplicate_report_accepts_legitimate_destination(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.txt").write_bytes(b"dup")
    (root / "b.txt").write_bytes(b"dup")
    analysis = analyze_duplicates(root)

    destination = tmp_path / "reports" / "duplicates.json"
    written = write_duplicate_report(analysis, destination)

    assert written == destination
    data = json.loads(destination.read_text())
    assert data["exact_duplicate_group_count"] == 1
    assert data["root"] == str(root)


def test_cryptomator_chunk_suffix_constant_is_lowercase_dotted() -> None:
    assert CRYPTOMATOR_CHUNK_SUFFIX == ".c9r"

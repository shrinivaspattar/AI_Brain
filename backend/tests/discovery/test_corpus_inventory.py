import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.discovery.corpus_inventory import (
    ARCHIVE_SUFFIXES,
    KNOWN_ARCHIVE_SUFFIXES,
    OTHER_ARCHIVE_SUFFIXES,
    scan_corpus,
    write_inventory_report,
)


def test_scan_rejects_nonexistent_root(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        scan_corpus(tmp_path / "does-not-exist")


def test_scan_rejects_file_root(tmp_path: Path) -> None:
    file_root = tmp_path / "file.txt"
    file_root.write_text("not a directory")

    with pytest.raises(NotADirectoryError):
        scan_corpus(file_root)


def test_scan_counts_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    nested = tmp_path / "nested" / "deeper"
    nested.mkdir(parents=True)
    (nested / "b.txt").write_text("world")

    inventory = scan_corpus(tmp_path)

    assert inventory.total_files == 2
    # root + "nested" + "nested/deeper" = 3 directories.
    assert inventory.total_directories == 3
    assert inventory.total_size_bytes == len("hello") + len("world")


def test_scan_classifies_by_extension(tmp_path: Path) -> None:
    (tmp_path / "photo.JPG").write_bytes(b"x" * 10)
    (tmp_path / "notes.txt").write_bytes(b"y" * 5)
    (tmp_path / "no_extension_file").write_bytes(b"z" * 3)

    inventory = scan_corpus(tmp_path)

    # Extension matching is case-insensitive.
    assert inventory.extension_counts[".jpg"] == 1
    assert inventory.extension_size_bytes[".jpg"] == 10
    assert inventory.extension_counts[".txt"] == 1
    assert inventory.extension_counts["(no extension)"] == 1
    assert inventory.extension_size_bytes["(no extension)"] == 3


def test_scan_recognizes_two_part_tar_extensions(tmp_path: Path) -> None:
    (tmp_path / "backup.tar.gz").write_bytes(b"x" * 20)
    (tmp_path / "plain.gz").write_bytes(b"y" * 8)

    inventory = scan_corpus(tmp_path)

    # A .tar.gz must be classified as its own extension, not folded
    # into plain .gz - materially different, much more common shape.
    assert inventory.extension_counts[".tar.gz"] == 1
    assert inventory.extension_counts[".gz"] == 1


def test_scan_classifies_known_vs_other_archives(tmp_path: Path) -> None:
    (tmp_path / "known.zip").write_bytes(b"x" * 10)
    (tmp_path / "known.7z").write_bytes(b"y" * 10)
    (tmp_path / "other.rar").write_bytes(b"z" * 10)
    (tmp_path / "other.iso").write_bytes(b"w" * 10)
    (tmp_path / "not_an_archive.txt").write_bytes(b"v" * 10)

    inventory = scan_corpus(tmp_path)

    assert inventory.known_archive_total == 2
    assert inventory.other_archive_total == 2
    assert inventory.archive_total_size_bytes == 40
    assert inventory.archive_counts[".zip"] == 1
    assert inventory.archive_counts[".rar"] == 1
    assert ".txt" not in inventory.archive_counts


def test_known_and_other_archive_suffixes_are_disjoint_and_union_correctly() -> None:
    assert KNOWN_ARCHIVE_SUFFIXES.isdisjoint(OTHER_ARCHIVE_SUFFIXES)
    assert ARCHIVE_SUFFIXES == KNOWN_ARCHIVE_SUFFIXES | OTHER_ARCHIVE_SUFFIXES


def test_scan_computes_directory_totals_bottom_up(tmp_path: Path) -> None:
    (tmp_path / "root_file.txt").write_bytes(b"x" * 10)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "sub_file.txt").write_bytes(b"y" * 20)
    subsub = sub / "subsub"
    subsub.mkdir()
    (subsub / "deep_file.txt").write_bytes(b"z" * 30)

    inventory = scan_corpus(tmp_path)

    by_path = {d.path: d.size_bytes for d in inventory.largest_directories}
    assert by_path[str(subsub)] == 30
    assert by_path[str(sub)] == 20 + 30  # includes subsub's total
    assert by_path[str(tmp_path)] == 10 + 20 + 30  # includes everything


def test_scan_top_n_bounds_largest_files_ranking(tmp_path: Path) -> None:
    for i in range(10):
        (tmp_path / f"file_{i}.bin").write_bytes(b"x" * (i + 1))

    inventory = scan_corpus(tmp_path, top_n=3)

    assert len(inventory.largest_files) == 3
    sizes = sorted((f.size_bytes for f in inventory.largest_files), reverse=True)
    assert sizes == [10, 9, 8]


def test_scan_top_n_zero_disables_ranking(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"x" * 10)

    inventory = scan_corpus(tmp_path, top_n=0)

    assert inventory.largest_files == []
    assert inventory.largest_directories == []
    # Aggregate counts are unaffected by disabling the rankings.
    assert inventory.total_files == 1
    assert inventory.total_size_bytes == 10


def test_scan_records_unreadable_file_without_aborting(tmp_path: Path) -> None:
    """A single entry that cannot be lstat'd (permission denied,
    vanished mid-scan, etc.) must be recorded in errors and skipped -
    never abort the whole scan."""
    (tmp_path / "good.txt").write_bytes(b"x" * 5)
    (tmp_path / "bad.txt").write_bytes(b"y" * 5)

    real_lstat = os.lstat

    def failing_lstat(path, *args, **kwargs):
        if str(path).endswith("bad.txt"):
            raise PermissionError(f"synthetic permission error for {path}")
        return real_lstat(path, *args, **kwargs)

    with patch("app.discovery.corpus_inventory.os.lstat", side_effect=failing_lstat):
        inventory = scan_corpus(tmp_path)

    assert inventory.total_files == 1
    assert inventory.total_size_bytes == 5
    assert len(inventory.errors) == 1
    assert "bad.txt" in inventory.errors[0].path
    assert "permission" in inventory.errors[0].message.lower()


def test_scan_records_unlistable_directory_without_aborting(tmp_path: Path) -> None:
    """A directory that cannot be listed at all (os.walk's own onerror
    path) must be recorded and the rest of the scan must still
    complete."""
    (tmp_path / "readable.txt").write_bytes(b"x" * 5)
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "hidden.txt").write_bytes(b"y" * 5)

    try:
        blocked.chmod(0o000)
        if os.access(blocked, os.R_OK):
            pytest.skip("running as a user that bypasses directory permissions")

        inventory = scan_corpus(tmp_path)

        assert inventory.total_files == 1  # only readable.txt was countable
        assert any("blocked" in e.path for e in inventory.errors)
    finally:
        blocked.chmod(0o755)


def test_scan_measures_symlink_itself_not_its_target(tmp_path: Path) -> None:
    """lstat, not stat: a symlink's OWN size is recorded, never the
    target's - and a symlink to a directory is never traversed into
    (os.walk(followlinks=False))."""
    target = tmp_path / "target.txt"
    target.write_bytes(b"x" * 1000)
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    inventory = scan_corpus(tmp_path)

    # Two files counted: the real target and the symlink itself - the
    # symlink's recorded size is its own (a short path string), not
    # 1000.
    assert inventory.total_files == 2
    link_entry = next(
        f for f in inventory.largest_files if f.path == str(link)
    )
    assert link_entry.size_bytes < 1000


def test_scan_never_follows_symlinked_directories(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "inside.txt").write_bytes(b"x" * 100)
    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    inventory = scan_corpus(tmp_path)

    # inside.txt is counted once, via the REAL directory - the
    # symlinked directory is never descended into a second time.
    assert inventory.total_files == 1
    assert inventory.total_size_bytes == 100


def test_write_inventory_report_creates_parent_dirs_and_valid_json(
    tmp_path: Path,
) -> None:
    (tmp_path / "corpus" / "a.txt").parent.mkdir(parents=True)
    (tmp_path / "corpus" / "a.txt").write_bytes(b"x" * 42)
    inventory = scan_corpus(tmp_path / "corpus")

    destination = tmp_path / "reports" / "nested" / "inventory.json"
    written = write_inventory_report(inventory, destination)

    assert written == destination
    assert destination.exists()
    data = json.loads(destination.read_text())
    assert data["total_files"] == 1
    assert data["total_size_bytes"] == 42
    assert data["root"] == str(tmp_path / "corpus")


def test_write_inventory_report_rejects_destination_equal_to_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.txt").write_bytes(b"x")
    inventory = scan_corpus(root)

    with pytest.raises(ValueError, match="scanned root"):
        write_inventory_report(inventory, root)

    # Nothing was written - the corpus itself must be untouched.
    assert list(root.iterdir()) == [root / "a.txt"]


def test_write_inventory_report_rejects_destination_directly_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    inventory = scan_corpus(root)

    with pytest.raises(ValueError, match="scanned root"):
        write_inventory_report(inventory, root / "report.json")

    assert list(root.iterdir()) == []


def test_write_inventory_report_rejects_destination_several_levels_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    inventory = scan_corpus(root)

    deep_destination = root / "a" / "b" / "c" / "report.json"

    with pytest.raises(ValueError, match="scanned root"):
        write_inventory_report(inventory, deep_destination)

    # The rejection must happen BEFORE any directory creation - a
    # refused write must leave the corpus completely untouched, not
    # just skip the final file write.
    assert not (root / "a").exists()


def test_write_inventory_report_rejects_dotdot_resolving_inside_root(
    tmp_path: Path,
) -> None:
    """A destination string that looks like it's OUTSIDE the root by
    naive prefix comparison, but resolves back inside it via `..`, must
    still be rejected - the check is on the RESOLVED path, never the
    raw string."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "sub").mkdir()
    inventory = scan_corpus(root)

    sneaky_destination = root / "sub" / ".." / "report.json"
    assert sneaky_destination.resolve(strict=False) == root / "report.json"

    with pytest.raises(ValueError, match="scanned root"):
        write_inventory_report(inventory, sneaky_destination)

    assert not (root / "report.json").exists()


def test_write_inventory_report_accepts_legitimate_destination_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.txt").write_bytes(b"x" * 7)
    inventory = scan_corpus(root)

    destination = tmp_path / "reports" / "inventory.json"
    written = write_inventory_report(inventory, destination)

    assert written == destination
    assert destination.exists()
    assert json.loads(destination.read_text())["total_files"] == 1


def test_write_inventory_report_still_creates_nested_parent_dirs_for_legitimate_destination(
    tmp_path: Path,
) -> None:
    """Regression check: the new safety validation must not interfere
    with the existing, legitimate parent-directory-creation behavior
    for a destination that is genuinely outside the root."""
    root = tmp_path / "corpus"
    root.mkdir()
    inventory = scan_corpus(root)

    destination = tmp_path / "reports" / "a" / "b" / "c" / "inventory.json"
    written = write_inventory_report(inventory, destination)

    assert written == destination
    assert destination.parent.is_dir()
    assert destination.exists()


def test_write_inventory_report_rejects_symlinked_directory_resolving_into_root(
    tmp_path: Path,
) -> None:
    """A destination directory that is ITSELF outside the root by raw
    path, but is a symlink pointing INSIDE the root, must still be
    rejected - resolution follows the symlink, exactly like the
    executor's own root-symlink handling elsewhere in this project."""
    root = tmp_path / "corpus"
    root.mkdir()
    inventory = scan_corpus(root)

    outside_dir = tmp_path / "looks_outside"
    outside_dir.symlink_to(root, target_is_directory=True)
    sneaky_destination = outside_dir / "report.json"

    assert sneaky_destination.resolve(strict=False) == root / "report.json"

    with pytest.raises(ValueError, match="scanned root"):
        write_inventory_report(inventory, sneaky_destination)

    assert not (root / "report.json").exists()


def test_inventory_json_dict_sorts_extension_and_archive_counts_descending(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_bytes(b"x")
    (tmp_path / "b.txt").write_bytes(b"y")
    (tmp_path / "c.txt").write_bytes(b"z")
    (tmp_path / "d.jpg").write_bytes(b"w")

    inventory = scan_corpus(tmp_path)
    data = inventory.to_json_dict()

    extensions_in_order = list(data["extension_counts"].keys())
    assert extensions_in_order[0] == ".txt"  # 3 occurrences, must sort first

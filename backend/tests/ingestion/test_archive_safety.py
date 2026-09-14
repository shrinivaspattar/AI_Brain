"""ArchiveExtractor-level safety tests added for Implementation
Milestone 5 (Archive Processing / Extraction): path traversal,
absolute member paths, ZIP symlink-mode entries, 7z junction/socket
rejection, and the post-extraction file-type audit fail-closed
backstop. No T7 access, no database - pure ArchiveExtractor unit
tests, matching the existing convention in this test module.
"""

import os
import stat
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import py7zr
import pytest

from app.ingestion.archive import ArchiveExtractor
from app.ingestion.scanner import DiscoveredFile


def _zip_file(path: Path) -> DiscoveredFile:
    return DiscoveredFile(path=path, relative_path=Path(path.name), size=path.stat().st_size)


# -- ZIP: traversal, absolute paths, symlink-mode entries -----------------


def test_rejects_zip_path_traversal(tmp_path: Path) -> None:
    zip_path = tmp_path / "evil.zip"
    with ZipFile(zip_path, "w") as zf:
        zf.writestr("../../etc/passwd", "pwned")

    with pytest.raises(ValueError, match="Unsafe ZIP member path"):
        ArchiveExtractor().extract([_zip_file(zip_path)], tmp_path / "extracted")


def test_rejects_zip_absolute_member_path(tmp_path: Path) -> None:
    """Verified, not assumed: Path's own "/" operator discards the left
    operand when the right is absolute, so `destination / member.filename`
    resolves to the absolute path itself - `is_relative_to(destination)`
    still correctly catches it (Milestone 5 design correction, section
    18's explicit "dedicated test" recommendation)."""
    zip_path = tmp_path / "evil.zip"
    with ZipFile(zip_path, "w") as zf:
        zf.writestr("/etc/passwd", "pwned")

    with pytest.raises(ValueError, match="Unsafe ZIP member path"):
        ArchiveExtractor().extract([_zip_file(zip_path)], tmp_path / "extracted")


def test_zip_symlink_mode_entry_extracts_as_regular_file_never_a_real_symlink(tmp_path: Path) -> None:
    """CONFIRMED EMPIRICALLY (Milestone 5 design correction pass): a ZIP
    entry crafted with external_attr marking it as a Unix symlink
    (S_IFLNK) is written by zipfile.extractall() as a REGULAR FILE
    containing the target string as literal content - never a real
    symlink. This is the exact reproduction from that investigation,
    now locked in as a real, committed test rather than relying on an
    unverified belief about stdlib behavior."""
    zip_path = tmp_path / "symlink_mode.zip"
    with ZipFile(zip_path, "w") as zf:
        info = ZipInfo("evil_link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")

    destination = tmp_path / "extracted"
    ArchiveExtractor().extract([_zip_file(zip_path)], destination)

    extracted_path = destination / "evil_link"
    result_stat = extracted_path.lstat()
    assert stat.S_ISREG(result_stat.st_mode)
    assert not stat.S_ISLNK(result_stat.st_mode)
    assert extracted_path.read_text() == "/etc/passwd"


# -- 7z: junction/socket rejection (corrected finding) ---------------------


def test_rejects_7z_member_that_is_neither_directory_file_nor_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """py7zr's real, public FileInfo dataclass (verified directly by
    introspection during the Milestone 5 implementation, correcting an
    earlier source-reading-only claim) has no is_junction/is_socket
    field - a junction, socket, or any other special type is instead
    represented by is_directory=is_file=is_symlink=False, ALL together.
    This test proves that combination is rejected, without needing to
    actually construct a real junction or socket inside a .7z."""
    archive_path = tmp_path / "special.7z"
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr(b"normal content", "normal.txt")

    destination = tmp_path / "extracted"
    extractor = ArchiveExtractor()

    real_list = py7zr.SevenZipFile.list

    def _fake_list(self):
        members = list(real_list(self))
        special = py7zr.FileInfo(
            filename="weird_entry",
            compressed=members[0].compressed,
            uncompressed=members[0].uncompressed,
            archivable=True,
            is_directory=False,
            is_file=False,
            is_symlink=False,
            creationtime=members[0].creationtime,
            crc32=members[0].crc32,
        )
        return [*members, special]

    monkeypatch.setattr(py7zr.SevenZipFile, "list", _fake_list)

    with pytest.raises(ValueError, match="neither a directory, file, nor symlink"):
        extractor.extract([_zip_file(archive_path)], destination)


# -- Post-extraction file-type audit (fail-closed backstop) ---------------


def test_post_extraction_audit_rejects_a_fifo_left_in_the_destination(tmp_path: Path) -> None:
    """Direct test of the fail-closed backstop itself: a FIFO placed in
    the extraction destination (simulating any future entry type no
    pre-extraction member check has been taught to name) must be
    rejected - the audit verifies the ACTUAL on-disk result, not merely
    the archive's declared member types."""
    zip_path = tmp_path / "safe.zip"
    with ZipFile(zip_path, "w") as zf:
        zf.writestr("normal.txt", "normal content")

    destination = tmp_path / "extracted"
    extractor = ArchiveExtractor()

    original_extractall = ZipFile.extractall

    def _extractall_then_plant_fifo(self, path=None, members=None, pwd=None):
        original_extractall(self, path, members, pwd)
        os.mkfifo(Path(path) / "sneaky_fifo")

    import unittest.mock as mock

    with mock.patch.object(ZipFile, "extractall", _extractall_then_plant_fifo):
        with pytest.raises(ValueError, match="unsafe extracted entry type"):
            extractor.extract([_zip_file(zip_path)], destination)


def test_post_extraction_audit_allows_ordinary_files_and_directories(tmp_path: Path) -> None:
    zip_path = tmp_path / "ordinary.zip"
    with ZipFile(zip_path, "w") as zf:
        zf.writestr("dir/nested.txt", "nested content")
        zf.writestr("top.txt", "top content")

    destination = tmp_path / "extracted"
    result = ArchiveExtractor().extract([_zip_file(zip_path)], destination)

    assert {f.relative_path.as_posix() for f in result if f.path != zip_path} == {"dir/nested.txt", "top.txt"}

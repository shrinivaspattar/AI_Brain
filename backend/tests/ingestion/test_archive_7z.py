from pathlib import Path
from shutil import _ntuple_diskusage

import py7zr
import pytest

from app.ingestion.archive import ArchiveExtractor
from app.ingestion.scanner import DiscoveredFile


def _write_7z(archive_path: Path, files: dict[str, bytes]) -> None:
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        for name, content in files.items():
            archive.writestr(content, name)


def test_extracts_7z_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "notes.7z"
    destination = tmp_path / "extracted"

    _write_7z(
        archive_path,
        {
            "notes.txt": b"hello AI_Brain",
            "docs/book.txt": b"book",
        },
    )

    discovered = DiscoveredFile(
        path=archive_path,
        relative_path=Path("notes.7z"),
        size=archive_path.stat().st_size,
    )

    result = ArchiveExtractor().extract([discovered], destination)

    assert (destination / "notes.txt").read_text() == "hello AI_Brain"
    assert (destination / "docs" / "book.txt").read_text() == "book"
    assert len(result) == 3  # original archive + 2 extracted files


def test_rejects_invalid_7z_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "fake.7z"
    archive_path.write_bytes(b"not a real 7z file")
    destination = tmp_path / "extracted"

    discovered = DiscoveredFile(
        path=archive_path,
        relative_path=Path("fake.7z"),
        size=archive_path.stat().st_size,
    )

    with pytest.raises(ValueError, match="Invalid 7Z archive"):
        ArchiveExtractor().extract([discovered], destination)


def _file_info(filename: str, **overrides) -> py7zr.FileInfo:
    defaults = dict(
        filename=filename,
        compressed=1,
        uncompressed=1,
        archivable=True,
        is_directory=False,
        is_file=True,
        is_symlink=False,
        creationtime=None,
        crc32=None,
    )
    defaults.update(overrides)
    return py7zr.FileInfo(**defaults)


def test_rejects_7z_path_traversal(tmp_path: Path) -> None:
    # py7zr's own writer refuses to create a path-traversal member
    # (check_archive_path), so a real malicious archive can't be built
    # through it - exercise the validator directly instead, matching the
    # existing convention for ZIP edge-case tests in test_archive.py.
    destination = tmp_path / "extracted"
    destination.mkdir()

    member = _file_info("../../etc/passwd")

    with pytest.raises(ValueError, match="Unsafe 7Z member path"):
        ArchiveExtractor()._validate_7z_members([member], destination)


def test_rejects_7z_symlink_members(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.7z"
    destination = tmp_path / "extracted"

    link_source = tmp_path / "link-source"
    link_target = tmp_path / "real-target.txt"
    link_target.write_text("real content")
    link_source.symlink_to(link_target)

    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.write(link_source, "escape-link")

    discovered = DiscoveredFile(
        path=archive_path,
        relative_path=Path("symlink.7z"),
        size=archive_path.stat().st_size,
    )

    with pytest.raises(ValueError, match="Unsafe 7Z member: symlink"):
        ArchiveExtractor().extract([discovered], destination)


def test_rejects_7z_extraction_below_hard_disk_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "safe.7z"
    destination = tmp_path / "extracted"

    _write_7z(archive_path, {"notes.txt": b"hello"})

    discovered = DiscoveredFile(
        path=archive_path,
        relative_path=Path("safe.7z"),
        size=archive_path.stat().st_size,
    )

    hard_reserve = ArchiveExtractor.HARD_FREE_SPACE_BYTES

    def fake_disk_usage(path: Path) -> _ntuple_diskusage:
        return _ntuple_diskusage(total=100 * 1024**3, used=0, free=hard_reserve)

    monkeypatch.setattr("app.ingestion.archive.disk_usage", fake_disk_usage)

    with pytest.raises(OSError, match="Insufficient disk space for 7Z extraction"):
        ArchiveExtractor().extract([discovered], destination)

    assert not (destination / "notes.txt").exists()


def test_rejects_7z_archive_above_hard_expansion_ratio(tmp_path: Path) -> None:
    archive_path = tmp_path / "bomb.7z"
    destination = tmp_path / "extracted"

    # highly compressible payload -> large expansion ratio once 7z's LZMA2
    # squeezes it down
    payload = b"\x00" * (5 * 1024 * 1024)
    _write_7z(archive_path, {"payload.bin": payload})

    compressed_size = archive_path.stat().st_size
    ratio = len(payload) / compressed_size
    assert ratio > ArchiveExtractor.HARD_EXPANSION_RATIO

    discovered = DiscoveredFile(
        path=archive_path,
        relative_path=Path("bomb.7z"),
        size=archive_path.stat().st_size,
    )

    with pytest.raises(ValueError, match="hard expansion ratio"):
        ArchiveExtractor().extract([discovered], destination)

    assert not (destination / "payload.bin").exists()


def test_discovers_7z_extracted_files_with_correct_sizes(tmp_path: Path) -> None:
    archive_path = tmp_path / "sized.7z"
    destination = tmp_path / "extracted"

    _write_7z(archive_path, {"a.txt": b"12345", "nested/b.txt": b"1234567890"})

    discovered = DiscoveredFile(
        path=archive_path,
        relative_path=Path("sized.7z"),
        size=archive_path.stat().st_size,
    )

    result = ArchiveExtractor().extract([discovered], destination)

    extracted = {f.relative_path: f.size for f in result if f.path != archive_path}

    assert extracted[Path("a.txt")] == 5
    assert extracted[Path("nested/b.txt")] == 10

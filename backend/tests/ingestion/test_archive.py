from pathlib import Path
from shutil import _ntuple_diskusage
from unittest.mock import MagicMock
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from app.ingestion.archive import ArchiveExtractor
from app.ingestion.scanner import DiscoveredFile


def test_rejects_extraction_below_hard_disk_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extraction should fail if it would cross the hard free-space reserve."""

    zip_path = tmp_path / "safe.zip"
    destination = tmp_path / "extracted"

    with ZipFile(zip_path, "w") as archive:
        archive.writestr("notes.txt", "hello")
        archive.writestr("documents/book.txt", "book")

    discovered = DiscoveredFile(
        path=zip_path,
        relative_path=Path("safe.zip"),
        size=zip_path.stat().st_size,
    )

    hard_reserve = ArchiveExtractor.HARD_FREE_SPACE_BYTES

    def fake_disk_usage(path: Path) -> _ntuple_diskusage:
        return _ntuple_diskusage(
            total=100 * 1024**3,
            used=0,
            free=hard_reserve + 8,
        )

    monkeypatch.setattr(
        "app.ingestion.archive.disk_usage",
        fake_disk_usage,
    )

    with pytest.raises(OSError, match="Insufficient disk space"):
        ArchiveExtractor().extract(
            [discovered],
            destination,
        )

    assert not (destination / "notes.txt").exists()
    assert not (destination / "documents" / "book.txt").exists()


def test_allows_extraction_at_exact_hard_disk_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extraction should be allowed when exactly at the hard reserve."""

    zip_path = tmp_path / "safe.zip"
    destination = tmp_path / "extracted"

    with ZipFile(zip_path, "w") as archive:
        archive.writestr("notes.txt", "hello")
        archive.writestr("documents/book.txt", "book")

    discovered = DiscoveredFile(
        path=zip_path,
        relative_path=Path("safe.zip"),
        size=zip_path.stat().st_size,
    )

    hard_reserve = ArchiveExtractor.HARD_FREE_SPACE_BYTES
    required = 9

    def fake_disk_usage(path: Path) -> _ntuple_diskusage:
        return _ntuple_diskusage(
            total=100 * 1024**3,
            used=0,
            free=hard_reserve + required,
        )

    monkeypatch.setattr(
        "app.ingestion.archive.disk_usage",
        fake_disk_usage,
    )

    result = ArchiveExtractor().extract(
        [discovered],
        destination,
    )

    assert len(result) == 3
    assert (destination / "notes.txt").read_text() == "hello"
    assert (destination / "documents" / "book.txt").read_text() == "book"


def test_rejects_member_above_hard_expansion_ratio(
    tmp_path: Path,
) -> None:
    """Archive members above the hard expansion ratio must be rejected."""

    zip_path = tmp_path / "bomb.zip"
    destination = tmp_path / "extracted"

    payload = b"\x00" * (2 * 1024 * 1024)

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("payload.bin", payload)

    with ZipFile(zip_path, "r") as archive:
        member = archive.getinfo("payload.bin")
        ratio = member.file_size / member.compress_size

    assert ratio > ArchiveExtractor.HARD_EXPANSION_RATIO

    discovered = DiscoveredFile(
        path=zip_path,
        relative_path=Path("bomb.zip"),
        size=zip_path.stat().st_size,
    )

    with pytest.raises(
        ValueError,
        match="hard expansion ratio",
    ):
        ArchiveExtractor().extract(
            [discovered],
            destination,
        )

    assert not (destination / "payload.bin").exists()


def test_allows_member_at_999x_expansion_ratio(
    tmp_path: Path,
) -> None:
    """Archive members at 999x expansion must be allowed."""

    zip_path = tmp_path / "ratio_999.zip"
    destination = tmp_path / "extracted"

    payload = b"\x00" * 999

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("payload.bin", payload)

    with ZipFile(zip_path, "r") as archive:
        member = archive.getinfo("payload.bin")
        ratio = member.file_size / member.compress_size

    assert ratio < ArchiveExtractor.HARD_EXPANSION_RATIO

    discovered = DiscoveredFile(
        path=zip_path,
        relative_path=Path("ratio_999.zip"),
        size=zip_path.stat().st_size,
    )

    result = ArchiveExtractor().extract(
        [discovered],
        destination,
    )

    assert (destination / "payload.bin").exists()
    assert len(result) == 2


def test_allows_member_at_exact_hard_expansion_ratio() -> None:
    """Archive members at exactly 1000x expansion must be allowed."""

    member = ZipInfo("payload.bin")
    member.file_size = 1000
    member.compress_size = 1

    archive = MagicMock()
    archive.infolist.return_value = [member]

    ArchiveExtractor()._validate_expansion(archive)


def test_rejects_member_with_zero_compressed_size() -> None:
    """A non-empty member with zero compressed size must be rejected."""

    member = ZipInfo("payload.bin")
    member.file_size = 1
    member.compress_size = 0

    archive = MagicMock()
    archive.infolist.return_value = [member]

    with pytest.raises(
        ValueError,
        match="hard expansion ratio",
    ):
        ArchiveExtractor()._validate_expansion(archive)

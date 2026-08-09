from pathlib import Path
from shutil import _ntuple_diskusage
from zipfile import ZipFile

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

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

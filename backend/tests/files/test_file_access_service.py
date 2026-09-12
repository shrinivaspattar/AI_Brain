from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.files.service import MAX_FILE_READ_LENGTH, FileAccessError, FileAccessService


def _service_with_roots(*roots: Path) -> FileAccessService:
    db = MagicMock()
    db.scalars.return_value = [str(root) for root in roots]
    return FileAccessService(db)


def test_read_file_returns_content_inside_allowed_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    file_path = source / "notes.txt"
    file_path.write_text("hello from an allowed directory")

    service = _service_with_roots(source)

    assert service.read_file(str(file_path)) == "hello from an allowed directory"


def test_read_file_rejects_path_outside_any_allowed_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    outside = tmp_path / "outside"
    outside.mkdir()
    file_path = outside / "secret.txt"
    file_path.write_text("should not be readable")

    service = _service_with_roots(source)

    with pytest.raises(FileAccessError, match="not inside any completed import job"):
        service.read_file(str(file_path))


def test_read_file_rejects_path_traversal_escaping_allowed_root(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("should not be readable")

    service = _service_with_roots(source)

    traversal_path = str(source / ".." / "outside" / "secret.txt")

    with pytest.raises(FileAccessError, match="not inside any completed import job"):
        service.read_file(traversal_path)


def test_read_file_rejects_missing_file_inside_allowed_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    service = _service_with_roots(source)

    with pytest.raises(FileAccessError, match="does not exist"):
        service.read_file(str(source / "missing.txt"))


def test_read_file_with_no_completed_import_jobs_rejects_everything(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "anything.txt"
    file_path.write_text("content")

    db = MagicMock()
    db.scalars.return_value = []
    service = FileAccessService(db)

    with pytest.raises(FileAccessError):
        service.read_file(str(file_path))


def test_read_file_truncates_content_over_the_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    file_path = source / "big.txt"
    file_path.write_text("x" * (MAX_FILE_READ_LENGTH + 500))

    service = _service_with_roots(source)

    content = service.read_file(str(file_path))

    assert content.endswith("... [truncated]")
    assert len(content) == MAX_FILE_READ_LENGTH + len("\n... [truncated]")


def test_read_file_rejects_binary_content_it_cannot_decode(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    file_path = source / "binary.dat"
    file_path.write_bytes(bytes(range(256)))

    service = _service_with_roots(source)

    with pytest.raises(FileAccessError, match="Could not read"):
        service.read_file(str(file_path))

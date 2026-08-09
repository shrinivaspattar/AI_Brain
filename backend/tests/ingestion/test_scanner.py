from pathlib import Path
import pytest

from app.ingestion.scanner import SourceScanner


def test_scan_discovers_files_recursively(tmp_path: Path) -> None:
    """Scanner should discover files in nested directories."""

    (tmp_path / "root.txt").write_text("root")
    nested = tmp_path / "documents" / "books"
    nested.mkdir(parents=True)
    (nested / "book.txt").write_text("book")

    result = SourceScanner().scan(tmp_path)

    assert [file.relative_path for file in result] == [
        Path("documents/books/book.txt"),
        Path("root.txt"),
    ]


def test_scan_rejects_missing_source(tmp_path: Path) -> None:
    """Scanner should reject a source directory that does not exist."""

    missing = tmp_path / "missing"

    with pytest.raises(FileNotFoundError):
        SourceScanner().scan(missing)


def test_scan_rejects_file_source(tmp_path: Path) -> None:
    """Scanner should reject a source path that is a file."""

    source = tmp_path / "source.txt"
    source.write_text("not a directory")

    with pytest.raises(NotADirectoryError):
        SourceScanner().scan(source)


def test_scan_records_file_metadata(tmp_path: Path) -> None:
    """Scanner should record correct path, relative path, and size."""

    content = "hello AI_Brain"
    source = tmp_path / "documents"
    source.mkdir()
    file_path = source / "notes.txt"
    file_path.write_text(content)

    result = SourceScanner().scan(tmp_path)

    assert len(result) == 1

    discovered = result[0]

    assert discovered.path == file_path.resolve()
    assert discovered.relative_path == Path("documents/notes.txt")
    assert discovered.size == len(content.encode())


def test_scan_returns_files_in_relative_path_order(tmp_path: Path) -> None:
    """Scanner should return discovered files in deterministic path order."""

    (tmp_path / "z.txt").write_text("z")
    (tmp_path / "a.txt").write_text("a")

    nested = tmp_path / "documents"
    nested.mkdir()
    (nested / "m.txt").write_text("m")

    result = SourceScanner().scan(tmp_path)

    assert [file.relative_path for file in result] == [
        Path("a.txt"),
        Path("documents/m.txt"),
        Path("z.txt"),
    ]


def test_scan_ignores_directories(tmp_path: Path) -> None:
    """Scanner should return files, not directories."""

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    nested_dir = tmp_path / "documents"
    nested_dir.mkdir()
    (nested_dir / "notes.txt").write_text("notes")

    result = SourceScanner().scan(tmp_path)

    assert [file.relative_path for file in result] == [
        Path("documents/notes.txt"),
    ]

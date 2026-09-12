import hashlib
from pathlib import Path
from unittest.mock import MagicMock

from app.ingestion.document_ingestor import DocumentIngestor, _hash_file
from app.models.document import Document


def test_ingest_scans_and_persists_documents(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "extracted"

    source.mkdir()
    destination.mkdir()

    file_path = source / "notes.txt"
    file_path.write_text("hello AI_Brain")

    document_service = MagicMock()
    document_service.create_document.side_effect = lambda data: Document(
        title=data.title,
        source=data.source,
        source_type=data.source_type,
        import_job_id=data.import_job_id,
    )

    ingestor = DocumentIngestor(
        document_service=document_service,
    )

    result = ingestor.ingest(source, destination)

    assert len(result) == 1

    document = result[0]

    assert document.title == "notes.txt"
    assert document.source == str(file_path.resolve())
    assert document.source_type == "txt"

    document_service.create_document.assert_called_once()


def test_ingest_uses_discovered_file_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "extracted"

    source.mkdir()
    destination.mkdir()

    (source / "README.md").write_text("# AI_Brain")

    document_service = MagicMock()

    ingestor = DocumentIngestor(
        document_service=document_service,
    )

    ingestor.ingest(source, destination)

    document_data = document_service.create_document.call_args.args[0]

    assert document_data.title == "README.md"
    assert document_data.source == str((source / "README.md").resolve())
    assert document_data.source_type == "md"
    assert document_data.import_job_id is None


def test_ingest_tags_documents_with_import_job_id(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "extracted"

    source.mkdir()
    destination.mkdir()

    (source / "notes.txt").write_text("hello AI_Brain")

    document_service = MagicMock()

    ingestor = DocumentIngestor(
        document_service=document_service,
    )

    ingestor.ingest(source, destination, import_job_id=99)

    document_data = document_service.create_document.call_args.args[0]

    assert document_data.import_job_id == 99


def test_hash_file_matches_hashlib_sha256(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("hello AI_Brain")

    expected = hashlib.sha256(b"hello AI_Brain").hexdigest()

    assert _hash_file(file_path) == expected


def test_hash_file_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert _hash_file(tmp_path / "missing.txt") is None


def test_hash_file_reads_large_files_in_chunks(tmp_path: Path) -> None:
    # Exercise the streaming read loop across multiple chunk boundaries,
    # not just a single small file.
    file_path = tmp_path / "big.bin"
    payload = b"x" * (1024 * 1024 * 3 + 17)  # 3MB + a partial chunk
    file_path.write_bytes(payload)

    assert _hash_file(file_path) == hashlib.sha256(payload).hexdigest()


def test_ingest_computes_content_hash(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "extracted"

    source.mkdir()
    destination.mkdir()

    (source / "notes.txt").write_text("hello AI_Brain")

    document_service = MagicMock()

    ingestor = DocumentIngestor(
        document_service=document_service,
    )

    ingestor.ingest(source, destination)

    document_data = document_service.create_document.call_args.args[0]

    assert document_data.content_hash == hashlib.sha256(b"hello AI_Brain").hexdigest()


def test_ingest_identical_files_get_identical_hashes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "extracted"

    source.mkdir()
    destination.mkdir()

    (source / "a.txt").write_text("duplicate content")
    (source / "b.txt").write_text("duplicate content")
    (source / "c.txt").write_text("different content")

    document_service = MagicMock()
    document_service.create_document.side_effect = lambda data: Document(
        title=data.title,
        source=data.source,
        source_type=data.source_type,
        content_hash=data.content_hash,
    )

    ingestor = DocumentIngestor(
        document_service=document_service,
    )

    result = ingestor.ingest(source, destination)

    hashes_by_title = {doc.title: doc.content_hash for doc in result}

    assert hashes_by_title["a.txt"] == hashes_by_title["b.txt"]
    assert hashes_by_title["a.txt"] != hashes_by_title["c.txt"]


def test_ingest_returns_empty_list_for_empty_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "extracted"

    source.mkdir()
    destination.mkdir()

    document_service = MagicMock()

    ingestor = DocumentIngestor(
        document_service=document_service,
    )

    result = ingestor.ingest(source, destination)

    assert result == []
    document_service.create_document.assert_not_called()


def test_ingest_skips_archive_container_documents(
    tmp_path: Path,
) -> None:
    from zipfile import ZipFile

    source = tmp_path / "source"
    destination = tmp_path / "extracted"

    source.mkdir()
    destination.mkdir()

    archive = source / "knowledge.zip"

    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("notes.txt", "hello from ZIP")

    document_service = MagicMock()

    ingestor = DocumentIngestor(
        document_service=document_service,
    )

    result = ingestor.ingest(source, destination)

    assert len(result) == 1

    document_data = document_service.create_document.call_args.args[0]

    assert document_data.title == "notes.txt"
    assert document_data.source_type == "txt"


def test_ingest_skips_7z_archive_container_documents(
    tmp_path: Path,
) -> None:
    import py7zr

    source = tmp_path / "source"
    destination = tmp_path / "extracted"

    source.mkdir()
    destination.mkdir()

    archive = source / "knowledge.7z"

    with py7zr.SevenZipFile(archive, "w") as sevenzip_file:
        sevenzip_file.writestr(b"hello from 7z", "notes.txt")

    document_service = MagicMock()

    ingestor = DocumentIngestor(
        document_service=document_service,
    )

    result = ingestor.ingest(source, destination)

    assert len(result) == 1

    document_data = document_service.create_document.call_args.args[0]

    assert document_data.title == "notes.txt"
    assert document_data.source_type == "txt"

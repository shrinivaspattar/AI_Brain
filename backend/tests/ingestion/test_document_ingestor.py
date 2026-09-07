from pathlib import Path
from unittest.mock import MagicMock

from app.ingestion.document_ingestor import DocumentIngestor
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

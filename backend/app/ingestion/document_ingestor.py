from pathlib import Path

from app.ingestion.archive import ArchiveExtractor
from app.ingestion.scanner import DiscoveredFile, SourceScanner
from app.models.document import Document
from app.schemas.document import DocumentCreate
from app.services.document_service import DocumentService


class DocumentIngestor:
    """Orchestrates filesystem discovery and document persistence."""

    def __init__(
        self,
        document_service: DocumentService,
        scanner: SourceScanner | None = None,
        archive_extractor: ArchiveExtractor | None = None,
    ):
        self.document_service = document_service
        self.scanner = scanner or SourceScanner()
        self.archive_extractor = archive_extractor or ArchiveExtractor()
        self.last_discovered_count = 0

    def ingest(
        self,
        source: Path,
        destination: Path,
        import_job_id: int | None = None,
    ) -> list[Document]:
        """Discover source files, expand archives, and persist documents."""

        discovered = self.scanner.scan(source)

        discovered = self.archive_extractor.extract(
            discovered,
            destination,
        )

        self.last_discovered_count = len(discovered)

        documents: list[Document] = []

        for file in discovered:
            if file.path.suffix.lower() == ".zip":
                continue

            document = self.document_service.create_document(
                self._build_document_data(file, import_job_id)
            )
            documents.append(document)

        return documents

    @staticmethod
    def _build_document_data(
        file: DiscoveredFile,
        import_job_id: int | None,
    ) -> DocumentCreate:
        return DocumentCreate(
            title=file.path.name,
            source=str(file.path),
            source_type=file.path.suffix.lower().lstrip(".") or "unknown",
            import_job_id=import_job_id,
        )

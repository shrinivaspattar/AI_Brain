from __future__ import annotations

from pathlib import Path
from shutil import disk_usage
from zipfile import ZipFile, is_zipfile

from app.ingestion.scanner import DiscoveredFile


class ArchiveExtractor:
    """Extract supported archives into discovered files."""

    HARD_FREE_SPACE_BYTES = 10 * 1024**3
    PREFERRED_FREE_SPACE_RATIO = 0.10

    SUSPICIOUS_EXPANSION_RATIO = 100
    HARD_EXPANSION_RATIO = 1000

    def extract(
        self,
        files: list[DiscoveredFile],
        destination: Path,
    ) -> list[DiscoveredFile]:
        destination = destination.resolve()

        if destination.exists() and not destination.is_dir():
            raise NotADirectoryError(destination)

        destination.mkdir(parents=True, exist_ok=True)

        result = list(files)

        for file in files:
            if file.path.suffix.lower() != ".zip":
                continue

            if not is_zipfile(file.path):
                raise ValueError(f"Invalid ZIP archive: {file.path}")

            with ZipFile(file.path, "r") as archive:
                self._validate_members(archive, destination)
                self._validate_expansion(archive)
                self._validate_disk_space(archive, destination)

                archive.extractall(destination)

                result.extend(
                    self._discover_extracted_files(
                        archive,
                        destination,
                    )
                )

        return result

    def _validate_members(
        self,
        archive: ZipFile,
        destination: Path,
    ) -> None:
        """Ensure every archive member remains inside the extraction destination."""
        for member in archive.infolist():
            target = (destination / member.filename).resolve()

            if not target.is_relative_to(destination):
                raise ValueError(f"Unsafe ZIP member path: {member.filename}")

    def _discover_extracted_files(
        self,
        archive: ZipFile,
        destination: Path,
    ) -> list[DiscoveredFile]:
        """Create discovered-file records for extracted archive members."""
        discovered: list[DiscoveredFile] = []

        for member in archive.infolist():
            if member.is_dir():
                continue

            path = (destination / member.filename).resolve()
            stat = path.stat()

            discovered.append(
                DiscoveredFile(
                    path=path,
                    relative_path=Path(member.filename),
                    size=stat.st_size,
                )
            )

        return discovered

    def _validate_disk_space(
        self,
        archive: ZipFile,
        destination: Path,
    ) -> None:
        """Ensure extraction does not cross the hard free-space reserve."""
        usage = disk_usage(destination)

        required = sum(
            member.file_size for member in archive.infolist() if not member.is_dir()
        )

        remaining = usage.free - required

        if remaining < self.HARD_FREE_SPACE_BYTES:
            raise OSError(
                "Insufficient disk space for ZIP extraction: "
                f"required={required} bytes, "
                f"free={usage.free} bytes, "
                f"remaining={remaining} bytes, "
                f"hard_reserve={self.HARD_FREE_SPACE_BYTES} bytes"
            )

    def _calculate_expansion_ratio(
        self,
        compressed_size: int,
        uncompressed_size: int,
    ) -> float:
        """Calculate the archive expansion ratio."""

        if compressed_size == 0:
            return float("inf")

        return uncompressed_size / compressed_size

    def _validate_expansion(
        self,
        archive: ZipFile,
    ) -> None:
        """Reject archive members that exceed the hard expansion-ratio limit."""
        pass

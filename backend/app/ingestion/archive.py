from __future__ import annotations

from pathlib import Path
from shutil import disk_usage
from zipfile import ZipFile, is_zipfile

import py7zr

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
            suffix = file.path.suffix.lower()

            if suffix == ".zip":
                result.extend(self._extract_zip(file, destination))
            elif suffix == ".7z":
                result.extend(self._extract_7z(file, destination))

        return result

    # -- ZIP -----------------------------------------------------------

    def _extract_zip(
        self,
        file: DiscoveredFile,
        destination: Path,
    ) -> list[DiscoveredFile]:
        if not is_zipfile(file.path):
            raise ValueError(f"Invalid ZIP archive: {file.path}")

        with ZipFile(file.path, "r") as archive:
            self._validate_members(archive, destination)
            self._validate_expansion(archive)
            self._validate_disk_space(archive, destination)

            archive.extractall(destination)

            return self._discover_extracted_files(archive, destination)

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

    def _validate_expansion(
        self,
        archive: ZipFile,
    ) -> None:
        """Reject archive members that exceed the hard expansion-ratio limit."""
        for member in archive.infolist():
            if member.is_dir():
                continue

            ratio = self._calculate_expansion_ratio(
                member.compress_size,
                member.file_size,
            )

            if ratio > self.HARD_EXPANSION_RATIO:
                raise ValueError(
                    "Archive member exceeds hard expansion ratio: "
                    f"member={member.filename}, "
                    f"ratio={ratio:.2f}, "
                    f"hard_limit={self.HARD_EXPANSION_RATIO}"
                )

    # -- 7Z --------------------------------------------------------------
    #
    # 7z's default "solid" compression shares one compressed block across
    # many members, so (unlike ZIP) most members have no meaningful
    # individual compressed size - py7zr.FileInfo.compressed is None for
    # all but (typically) the first member of a block. The expansion-ratio
    # bomb check is therefore done at the whole-archive level: total
    # uncompressed member size vs. the .7z file's actual size on disk.

    def _extract_7z(
        self,
        file: DiscoveredFile,
        destination: Path,
    ) -> list[DiscoveredFile]:
        if not py7zr.is_7zfile(file.path):
            raise ValueError(f"Invalid 7Z archive: {file.path}")

        with py7zr.SevenZipFile(file.path, "r") as archive:
            members = archive.list()

            self._validate_7z_members(members, destination)
            self._validate_7z_expansion(file.path, members)
            self._validate_7z_disk_space(members, destination)

            archive.extractall(destination)

            return self._discover_extracted_7z_files(members, destination)

    def _validate_7z_members(
        self,
        members: list[py7zr.FileInfo],
        destination: Path,
    ) -> None:
        """Ensure every member is a plain file/dir within the destination.

        Symlinks are rejected outright: py7zr recreates real OS symlinks on
        extraction (ZipFile does not), so an archive-controlled symlink
        could otherwise point outside the destination.
        """
        for member in members:
            if member.is_symlink:
                raise ValueError(f"Unsafe 7Z member: symlink {member.filename}")

            target = (destination / member.filename).resolve()

            if not target.is_relative_to(destination):
                raise ValueError(f"Unsafe 7Z member path: {member.filename}")

    def _validate_7z_expansion(
        self,
        archive_path: Path,
        members: list[py7zr.FileInfo],
    ) -> None:
        """Reject archives that exceed the hard expansion-ratio limit."""
        compressed_size = archive_path.stat().st_size
        uncompressed_size = sum(
            member.uncompressed for member in members if not member.is_directory
        )

        ratio = self._calculate_expansion_ratio(compressed_size, uncompressed_size)

        if ratio > self.HARD_EXPANSION_RATIO:
            raise ValueError(
                "Archive exceeds hard expansion ratio: "
                f"archive={archive_path}, "
                f"ratio={ratio:.2f}, "
                f"hard_limit={self.HARD_EXPANSION_RATIO}"
            )

    def _validate_7z_disk_space(
        self,
        members: list[py7zr.FileInfo],
        destination: Path,
    ) -> None:
        """Ensure extraction does not cross the hard free-space reserve."""
        usage = disk_usage(destination)

        required = sum(
            member.uncompressed for member in members if not member.is_directory
        )

        remaining = usage.free - required

        if remaining < self.HARD_FREE_SPACE_BYTES:
            raise OSError(
                "Insufficient disk space for 7Z extraction: "
                f"required={required} bytes, "
                f"free={usage.free} bytes, "
                f"remaining={remaining} bytes, "
                f"hard_reserve={self.HARD_FREE_SPACE_BYTES} bytes"
            )

    def _discover_extracted_7z_files(
        self,
        members: list[py7zr.FileInfo],
        destination: Path,
    ) -> list[DiscoveredFile]:
        """Create discovered-file records for extracted archive members."""
        discovered: list[DiscoveredFile] = []

        for member in members:
            if member.is_directory:
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

    # -- shared ------------------------------------------------------------

    def _calculate_expansion_ratio(
        self,
        compressed_size: int,
        uncompressed_size: int,
    ) -> float:
        """Calculate the archive expansion ratio."""

        if compressed_size == 0:
            return float("inf")

        return uncompressed_size / compressed_size

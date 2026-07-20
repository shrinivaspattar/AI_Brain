from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class DiscoveredFile:
    """
    Metadata describing a discovered file.

    Attributes:
        path: Absolute filesystem path.
        relative_path: Path relative to the scanned root.
        size: File size in bytes.
    """

    path: Path
    relative_path: Path
    size: int


class SourceScanner:
    """Recursively discovers files within a source directory."""

    def scan(self, source: Path) -> list[DiscoveredFile]:
        """
        Scan a directory recursively.

        Args:
            source:
                Root directory to scan.

        Returns:
            List of discovered files.

        Raises:
            FileNotFoundError:
                If the source directory does not exist.

            NotADirectoryError:
                If the source path is not a directory.
        """
        source = source.resolve()

        if not source.exists():
            raise FileNotFoundError(source)

        if not source.is_dir():
            raise NotADirectoryError(source)

        discovered: list[DiscoveredFile] = []

        for path in source.rglob("*"):
            if not path.is_file():
                continue

            stat = path.stat()

            discovered.append(
                DiscoveredFile(
                    path=path,
                    relative_path=path.relative_to(source),
                    size=stat.st_size,
                )
            )

        return sorted(discovered, key=lambda f: str(f.relative_path))

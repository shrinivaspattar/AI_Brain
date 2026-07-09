from pathlib import Path

ARCHIVE_EXTENSIONS = {
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
}


def is_archive(path: Path) -> bool:
    """Return True if the file is a supported archive."""
    return path.suffix.lower() in ARCHIVE_EXTENSIONS
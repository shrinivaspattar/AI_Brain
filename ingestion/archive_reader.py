import io
import zipfile
from pathlib import Path

ARCHIVE_EXTENSIONS = {
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
}

def open_zip(path: Path):
    """Open a ZIP file from disk."""
    return zipfile.ZipFile(path, "r")


def open_zip_bytes(data: bytes):
    """Open a ZIP archive from bytes in memory."""
    return zipfile.ZipFile(io.BytesIO(data), "r")


def list_zip_contents_from_archive(archive):
    """
    Return metadata for every file inside an already-open ZIP archive.
    """

    contents = []

    for item in archive.infolist():

        contents.append(
            {
                "name": item.filename,
                "size": item.file_size,
                "compressed": item.compress_size,
                "is_dir": item.is_dir(),
                "is_archive": Path(item.filename).suffix.lower() in ARCHIVE_EXTENSIONS,
            }
        )

    return contents


def list_zip_contents(path: Path):
    """
    Open a ZIP file from disk and return its contents.
    """
    with open_zip(path) as archive:
        return list_zip_contents_from_archive(archive)
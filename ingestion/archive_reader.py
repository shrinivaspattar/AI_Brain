import zipfile
from pathlib import Path


def list_zip_contents(path: Path):
    """Return a list of files inside a ZIP archive."""

    with zipfile.ZipFile(path, "r") as archive:
        return archive.namelist()
import zipfile
from pathlib import Path


def list_zip_contents(path: Path):
    """
    Return metadata for every file inside a ZIP archive.
    """

    contents = []

    with zipfile.ZipFile(path, "r") as archive:

        for item in archive.infolist():

            contents.append(
                {
                    "name": item.filename,
                    "size": item.file_size,
                    "compressed": item.compress_size,
                    "is_dir": item.is_dir(),
                }
            )

    return contents
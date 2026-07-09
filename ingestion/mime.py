import mimetypes
from pathlib import Path


def detect_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path)

    if mime is None:
        return "application/octet-stream"

    return mime
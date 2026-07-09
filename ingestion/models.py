from dataclasses import dataclass


@dataclass
class FileRecord:
    path: str
    name: str
    extension: str
    size: int
    modified: str

    sha256: str = ""
    mime: str = ""
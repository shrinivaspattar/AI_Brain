from dataclasses import dataclass
from pathlib import Path
from datetime import datetime


@dataclass
class FileRecord:
    path: Path
    filename: str
    extension: str
    size: int
    modified: datetime

from pathlib import Path
from datetime import datetime

from models import FileRecord


def read_metadata(path: Path) -> FileRecord:
    stat = path.stat()

    return FileRecord(
        path=path,
        filename=path.name,
        extension=path.suffix.lower(),
        size=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime),
    )

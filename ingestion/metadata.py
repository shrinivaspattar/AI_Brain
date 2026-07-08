from pathlib import Path
from datetime import datetime


def get_metadata(path: Path):
    stat = path.stat()

    return {
        "name": path.name,
        "extension": path.suffix.lower(),
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime),
        "path": str(path),
    }
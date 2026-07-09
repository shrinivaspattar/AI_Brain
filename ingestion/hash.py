import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hash of a file."""

    sha = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)  # 1 MB chunks
            if not chunk:
                break
            sha.update(chunk)

    return sha.hexdigest()
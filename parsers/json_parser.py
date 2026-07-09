from pathlib import Path


def extract(path: Path):
    return path.read_text(errors="ignore")
from __future__ import annotations

from pathlib import Path

import docx
import pypdf


def extract_text(path: Path) -> str:
    """Extract plain text from a file, dispatching by extension.

    Any extension not explicitly handled falls back to reading the file
    as UTF-8 text — this covers plain text, markdown, code, etc. and
    raises (UnicodeDecodeError / OSError) for anything that isn't
    decodable text, such as binaries or missing files.
    """
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return _extract_pdf(path)

    if suffix == ".docx":
        return _extract_docx(path)

    return path.read_text(encoding="utf-8")


def _extract_pdf(path: Path) -> str:
    reader = pypdf.PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx(path: Path) -> str:
    document = docx.Document(str(path))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)

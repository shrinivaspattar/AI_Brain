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

    A conflict-resolution/dedup tool suffix appended with no separating
    dot (e.g. "report.pdf_1768918262", observed for real in a backup
    corpus) is tolerated: ".pdf"/".docx" is matched by prefix, not exact
    equality, so these still get proper extraction instead of silently
    falling through to the UTF-8 fallback and failing to decode.
    """
    suffix = path.suffix.lower()

    if suffix == ".pdf" or suffix.startswith(".pdf_"):
        return _extract_pdf(path)

    if suffix == ".docx" or suffix.startswith(".docx_"):
        return _extract_docx(path)

    return path.read_text(encoding="utf-8")


def _extract_pdf(path: Path) -> str:
    reader = pypdf.PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx(path: Path) -> str:
    document = docx.Document(str(path))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)

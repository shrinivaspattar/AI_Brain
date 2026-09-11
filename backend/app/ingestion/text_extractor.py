from __future__ import annotations

from pathlib import Path

import docx
import openpyxl
import pptx
import pypdf


def _extract_pdf(path: Path) -> str:
    reader = pypdf.PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx(path: Path) -> str:
    document = docx.Document(str(path))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def _extract_pptx(path: Path) -> str:
    presentation = pptx.Presentation(str(path))

    lines: list[str] = []

    for slide in presentation.slides:
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text:
                lines.append(shape.text_frame.text)

    return "\n".join(lines)


def _extract_xlsx(path: Path) -> str:
    # openpyxl validates the *filename's* extension internally and refuses
    # anything but .xlsx/.xlsm/.xltx/.xltm when given a path string - that
    # check is skipped when given a file object instead, which is what
    # lets a conflict-suffix-renamed file (e.g. ".xlsx_1768918262") through.
    with path.open("rb") as fh:
        workbook = openpyxl.load_workbook(fh, read_only=True, data_only=True)

        try:
            lines: list[str] = []

            for sheet in workbook.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    cells = [str(cell) for cell in row if cell is not None]

                    if cells:
                        lines.append("\t".join(cells))

            return "\n".join(lines)

        finally:
            workbook.close()


_EXTRACTORS = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".pptx": _extract_pptx,
    ".xlsx": _extract_xlsx,
}


def extract_text(path: Path) -> str:
    """Extract plain text from a file, dispatching by extension.

    Any extension not explicitly handled falls back to reading the file
    as UTF-8 text — this covers plain text, markdown, code, etc. and
    raises (UnicodeDecodeError / OSError) for anything that isn't
    decodable text, such as binaries or missing files.

    A conflict-resolution/dedup tool suffix appended with no separating
    dot (e.g. "report.pdf_1768918262", observed for real in a backup
    corpus) is tolerated: a known extension is matched by prefix, not
    exact equality, so these still get proper extraction instead of
    silently falling through to the UTF-8 fallback and failing to decode.
    """
    suffix = path.suffix.lower()

    for extension, extractor in _EXTRACTORS.items():
        if suffix == extension or suffix.startswith(extension + "_"):
            return extractor(path)

    return path.read_text(encoding="utf-8")

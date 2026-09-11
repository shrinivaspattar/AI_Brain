from pathlib import Path
from unittest.mock import MagicMock, patch

import docx
import pytest

from app.ingestion.text_extractor import extract_text


def test_extract_text_reads_plain_text_files(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("hello AI_Brain")

    assert extract_text(file_path) == "hello AI_Brain"


def test_extract_text_reads_unlisted_extensions_as_plain_text(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "data.csv"
    file_path.write_text("a,b,c\n1,2,3")

    assert extract_text(file_path) == "a,b,c\n1,2,3"


def test_extract_text_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        extract_text(tmp_path / "missing.txt")


def test_extract_text_raises_for_binary_content(tmp_path: Path) -> None:
    file_path = tmp_path / "image.bin"
    file_path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\xff\xfe")

    with pytest.raises(UnicodeDecodeError):
        extract_text(file_path)


def test_extract_text_joins_pdf_pages() -> None:
    page_one = MagicMock()
    page_one.extract_text.return_value = "page one"

    page_two = MagicMock()
    page_two.extract_text.return_value = "page two"

    with patch("app.ingestion.text_extractor.pypdf.PdfReader") as reader_class:
        reader_class.return_value.pages = [page_one, page_two]

        result = extract_text(Path("document.pdf"))

    assert result == "page one\npage two"
    reader_class.assert_called_once_with("document.pdf")


def test_extract_text_handles_pdf_pages_with_no_extractable_text() -> None:
    page = MagicMock()
    page.extract_text.return_value = None

    with patch("app.ingestion.text_extractor.pypdf.PdfReader") as reader_class:
        reader_class.return_value.pages = [page]

        result = extract_text(Path("scanned.pdf"))

    assert result == ""


def test_extract_text_reads_docx_paragraphs(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.docx"

    document = docx.Document()
    document.add_paragraph("First paragraph.")
    document.add_paragraph("Second paragraph.")
    document.save(str(file_path))

    assert extract_text(file_path) == "First paragraph.\nSecond paragraph."


def test_extract_text_handles_pdf_renamed_with_conflict_suffix() -> None:
    # Real-world pattern: a bulk conflict-resolution/dedup rename appends
    # "_<timestamp>" directly after the extension, no separating dot -
    # e.g. "Sept-Oct-2022 - 2nd Sem M Tech VLSI-min.pdf_1768918262".
    page = MagicMock()
    page.extract_text.return_value = "exam questions"

    with patch("app.ingestion.text_extractor.pypdf.PdfReader") as reader_class:
        reader_class.return_value.pages = [page]

        result = extract_text(Path("question-paper.pdf_1768918262"))

    assert result == "exam questions"
    reader_class.assert_called_once_with("question-paper.pdf_1768918262")


def test_extract_text_handles_docx_renamed_with_conflict_suffix(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "notes.docx_1768918262"

    document = docx.Document()
    document.add_paragraph("Renamed but still a real docx.")
    document.save(str(file_path))

    assert extract_text(file_path) == "Renamed but still a real docx."


def test_extract_text_does_not_misfire_on_unrelated_extension_prefix(
    tmp_path: Path,
) -> None:
    # Guard against overly broad matching: ".pdfx" or similar must not be
    # treated as a renamed PDF.
    file_path = tmp_path / "notes.pdfx"
    file_path.write_text("just text, not a pdf")

    assert extract_text(file_path) == "just text, not a pdf"

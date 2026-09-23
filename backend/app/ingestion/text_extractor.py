from __future__ import annotations

import re
from pathlib import Path

import docx
import openpyxl
import pptx
import pypdf
import yaml


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


# Obsidian's standard note header: a YAML mapping between two `---` lines
# at the very start of the file. Common in web-clipper exports especially
# (title/source/author/published/created/tags) - confirmed against this
# project's own ingested corpus before building this (see the 2026-09-23
# session note), not assumed.
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?\r?\n)---\r?\n?", re.DOTALL)

# Obsidian's link syntax: [[Note]], [[Note|Display text]], and the embed
# form ![[target]] (another note, an image, etc.). `[[` is otherwise rare
# in real prose, but numeric/array-like content (e.g. "[[1, 2], [3, 4]]")
# can coincidentally match double-bracket syntax - harmless here since the
# regex just reformats whatever is inside without checking it is a real
# note name.
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")

def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Splits a leading YAML frontmatter block from the rest of the text.
    Returns (fields, body); fields is None (and text returned unchanged)
    when there is no frontmatter block, it isn't valid YAML, or it isn't a
    mapping - a malformed or unusual header must never break ingestion of
    the file's actual content."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    try:
        fields = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None, text
    if not isinstance(fields, dict):
        return None, text
    return fields, text[match.end():]


def _format_frontmatter_value(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _format_frontmatter_header(fields: dict) -> str:
    """Every present, non-empty field as a plain 'Label: value' line, in
    the order it appears in the note - whatever fields a person's own
    frontmatter happens to have (title, date, status, tags, ...), not a
    curated subset. Formatting only, no schema imposed."""
    lines = [
        f"{str(key).replace('_', ' ').strip().capitalize()}: {_format_frontmatter_value(value)}"
        for key, value in fields.items()
        if value not in (None, "", [], {})
    ]
    return "\n".join(lines)


def _clean_wikilinks(text: str) -> str:
    """Rewrites [[Note]]/[[Note|Display]] to the plain text a reader would
    actually see rendered (the display text if given, else the note name),
    and drops ![[embed]] entirely - an embedded image/note's target name
    alone isn't meaningful content to chunk."""

    def replace(match: re.Match) -> str:
        is_embed, target, display = match.group(1), match.group(2), match.group(3)
        if is_embed:
            return ""
        return (display or target).strip()

    return _WIKILINK_RE.sub(replace, text)


def _clean_obsidian_markdown(text: str) -> str:
    """Obsidian-aware cleanup for a .md file's extracted text: frontmatter
    reformatted as a short readable header (instead of chunked as raw
    YAML), and [[wikilinks]]/![[embeds]] rewritten to plain text (instead
    of chunked as literal bracket syntax). Everything else is left as-is -
    this is text cleanup, not link resolution or a knowledge graph."""
    fields, body = _split_frontmatter(text)
    body = _clean_wikilinks(body)

    if fields:
        header = _format_frontmatter_header(fields)
        if header:
            return f"{header}\n\n{body.strip()}"

    return body


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
            return _without_nul(extractor(path))

    text = _read_text_file(path)

    if suffix == ".md" or suffix.startswith(".md_"):
        text = _clean_obsidian_markdown(text)

    return _without_nul(text)


def _read_text_file(path: Path) -> str:
    """Plain text as UTF-8, or as UTF-16 when the file starts with a UTF-16
    byte-order mark (common for text saved by Windows tools). Anything else
    that is not valid UTF-8 still raises, so binary files are not read as
    garbage text."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8")


def _without_nul(text: str) -> str:
    """PostgreSQL text columns cannot store the NUL character (0x00), and
    it carries no meaning in extracted text. Real PDFs and slide decks
    contain it (found on the first real-data pilot); leaving it in made the
    chunk insert fail for the whole run."""
    return text.replace("\x00", "")

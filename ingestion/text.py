from parsers import (
    text_parser,
    markdown_parser,
    pdf_parser,
    json_parser,
)
from parsers import docx_parser


def extract_text(path):
    suffix = path.suffix.lower()

    if suffix == ".txt":
        return text_parser.extract(path)

    if suffix == ".md":
        return markdown_parser.extract(path)

    if suffix == ".pdf":
        return pdf_parser.extract(path)

    if suffix == ".json":
        return json_parser.extract(path)
    if suffix == ".docx":
        return docx_parser.extract(path)

    return ""
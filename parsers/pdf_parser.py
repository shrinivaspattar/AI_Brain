from pathlib import Path
from pypdf import PdfReader


def extract(path: Path):
    try:
        reader = PdfReader(path)
        text = ""

        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"

        return text

    except Exception as e:
        print(f"Warning: Could not read PDF '{path.name}': {e}")
        return ""
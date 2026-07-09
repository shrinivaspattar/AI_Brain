from pathlib import Path
from pypdf import PdfReader


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix in {".txt", ".md"}:
        return path.read_text(
            encoding="utf-8",
            errors="ignore",
        )

    elif path.suffix.lower() == ".pdf":
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
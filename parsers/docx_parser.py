from docx import Document


def extract(path):
    try:
        doc = Document(path)

        text = []

        for paragraph in doc.paragraphs:
            text.append(paragraph.text)

        return "\n".join(text)

    except Exception as e:
        print(f"Warning: Could not read DOCX '{path.name}': {e}")
        return ""
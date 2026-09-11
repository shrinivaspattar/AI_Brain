from __future__ import annotations

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split text into overlapping, character-bounded chunks.

    Consecutive chunks share `overlap` characters so context isn't lost
    at a chunk boundary.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")

    text = text.strip()

    if not text:
        return []

    chunks: list[str] = []
    start = 0
    length = len(text)
    stride = chunk_size - overlap

    while start < length:
        end = min(start + chunk_size, length)
        chunks.append(text[start:end])

        if end == length:
            break

        start += stride

    return chunks

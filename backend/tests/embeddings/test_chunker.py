import pytest

from app.embeddings.chunker import chunk_text


def test_chunk_text_returns_empty_list_for_empty_input() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_chunk_text_returns_single_chunk_when_shorter_than_chunk_size() -> None:
    result = chunk_text("hello AI_Brain", chunk_size=1000, overlap=200)

    assert result == ["hello AI_Brain"]


def test_chunk_text_splits_long_text_with_overlap() -> None:
    text = "".join(chr(ord("a") + (i % 26)) for i in range(25))

    result = chunk_text(text, chunk_size=10, overlap=3)

    assert result == [
        text[0:10],
        text[7:17],
        text[14:24],
        text[21:25],
    ]
    assert result[0][-3:] == result[1][:3]
    assert result[1][-3:] == result[2][:3]


def test_chunk_text_rejects_non_positive_chunk_size() -> None:
    with pytest.raises(ValueError, match="chunk_size must be positive"):
        chunk_text("hello", chunk_size=0)


def test_chunk_text_rejects_overlap_not_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError, match="overlap must be non-negative"):
        chunk_text("hello", chunk_size=10, overlap=10)

    with pytest.raises(ValueError, match="overlap must be non-negative"):
        chunk_text("hello", chunk_size=10, overlap=-1)

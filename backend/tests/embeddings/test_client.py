from unittest.mock import MagicMock, patch

import pytest

from app.embeddings.client import EmbeddingClient, EmbeddingUnavailableError


def test_embed_returns_empty_list_for_no_texts() -> None:
    with patch("app.embeddings.client.ollama.Client"):
        client = EmbeddingClient()

        assert client.embed([]) == []


def test_embed_returns_vectors_in_order() -> None:
    with patch("app.embeddings.client.ollama.Client") as client_class:
        response = MagicMock()
        response.embeddings = [[0.1, 0.2], [0.3, 0.4]]
        client_class.return_value.embed.return_value = response

        client = EmbeddingClient(model="nomic-embed-text", host="http://ollama:11434")

        result = client.embed(["hello", "world"])

        assert result == [[0.1, 0.2], [0.3, 0.4]]

        client_class.return_value.embed.assert_called_once_with(
            model="nomic-embed-text",
            input=["hello", "world"],
        )


def test_embed_client_defaults_from_settings() -> None:
    with patch("app.embeddings.client.ollama.Client") as client_class:
        from app.core.config import settings

        EmbeddingClient()

        client_class.assert_called_once_with(host=settings.OLLAMA_HOST)


def test_embed_raises_embedding_unavailable_error_on_underlying_failure() -> None:
    with patch("app.embeddings.client.ollama.Client") as client_class:
        underlying = ConnectionError("connection refused")
        client_class.return_value.embed.side_effect = underlying

        client = EmbeddingClient()

        with pytest.raises(EmbeddingUnavailableError) as excinfo:
            client.embed(["hello"])

        assert str(excinfo.value) == "connection refused"
        assert excinfo.value.__cause__ is underlying

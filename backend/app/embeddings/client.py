from __future__ import annotations

import ollama

from app.core.config import settings


class EmbeddingUnavailableError(RuntimeError):
    """Raised when the Ollama embed API can't be reached or fails."""


class EmbeddingClient:
    """Thin wrapper around the Ollama embed API."""

    def __init__(
        self,
        model: str | None = None,
        host: str | None = None,
    ):
        self.model = model or settings.EMBEDDING_MODEL
        self._client = ollama.Client(host=host or settings.OLLAMA_HOST)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, preserving input order."""
        if not texts:
            return []

        try:
            response = self._client.embed(model=self.model, input=texts)
        except Exception as exc:
            raise EmbeddingUnavailableError(str(exc)) from exc

        return [list(embedding) for embedding in response.embeddings]

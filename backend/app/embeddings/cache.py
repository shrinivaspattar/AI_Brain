from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache
from typing import Protocol

from app.core.config import settings
from app.embeddings.client import EmbeddingClient

logger = logging.getLogger(__name__)

# Short timeouts so a dead or unreachable Redis costs a request a fraction of
# a second at most, never a hang. (Connection-refused fails immediately; these
# bound the unreachable-host case.)
_REDIS_TIMEOUT_SECONDS = 0.25


class KeyValueStore(Protocol):
    """The two Redis operations the cache needs; lets tests inject a fake."""

    def get(self, key: str): ...

    def set(self, key: str, value: str, ex: int | None = None): ...


def cache_key(model: str, text: str) -> str:
    """Keyed by model AND text hash: a different embedding model must never
    be served another model's vectors."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"embedding:{model}:{digest}"


class CachedEmbeddingClient:
    """Read-through cache for query embeddings, with the same `embed()` shape
    as `EmbeddingClient`.

    FAILS OPEN: any store error (Redis down, timeout, corrupt or
    wrong-dimension cached value) is logged and treated as a cache miss, so
    the app behaves exactly as it does without a cache. The cache can make a
    request faster; it can never make one fail.

    Used for QUERY embeddings only (retrieval search). Ingestion embeds
    each chunk once by design, so caching it would add nothing.
    """

    def __init__(self, inner: EmbeddingClient, store: KeyValueStore, ttl_seconds: int):
        self._inner = inner
        self._store = store
        self._ttl_seconds = ttl_seconds
        self.hits = 0
        self.misses = 0

    @property
    def model(self) -> str:
        return self._inner.model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        results: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []

        for index, text in enumerate(texts):
            cached = self._lookup(text)
            if cached is None:
                missing.append(index)
            else:
                results[index] = cached
                self.hits += 1

        if missing:
            self.misses += len(missing)
            fresh = self._inner.embed([texts[i] for i in missing])
            for index, vector in zip(missing, fresh):
                results[index] = vector
                self._save(texts[index], vector)

        return results  # type: ignore[return-value]

    def _lookup(self, text: str) -> list[float] | None:
        try:
            raw = self._store.get(cache_key(self.model, text))
            if raw is None:
                return None
            vector = json.loads(raw)
            if (
                not isinstance(vector, list)
                or len(vector) != settings.EMBEDDING_DIMENSIONS
                or not all(isinstance(x, (int, float)) for x in vector)
            ):
                return None
            return [float(x) for x in vector]
        except Exception as exc:
            logger.warning("Embedding cache lookup failed (%s: %s); treating as a miss", type(exc).__name__, exc)
            return None

    def _save(self, text: str, vector: list[float]) -> None:
        try:
            self._store.set(cache_key(self.model, text), json.dumps(vector), ex=self._ttl_seconds)
        except Exception as exc:
            logger.warning("Embedding cache write failed (%s: %s); continuing without caching", type(exc).__name__, exc)


@lru_cache(maxsize=1)
def _redis_store(url: str):
    import redis

    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
        socket_timeout=_REDIS_TIMEOUT_SECONDS,
    )


def build_query_embedding_client() -> EmbeddingClient | CachedEmbeddingClient:
    """The client retrieval uses for query embeddings. A plain EmbeddingClient
    unless EMBEDDING_CACHE_ENABLED is set, so default behavior is unchanged."""
    inner = EmbeddingClient()
    if not settings.EMBEDDING_CACHE_ENABLED:
        return inner
    return CachedEmbeddingClient(
        inner,
        _redis_store(settings.REDIS_URL),
        settings.EMBEDDING_CACHE_TTL_SECONDS,
    )

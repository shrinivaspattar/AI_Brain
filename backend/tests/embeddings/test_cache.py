import json
import uuid

import pytest

from app.core.config import settings
from app.embeddings import cache as cache_module
from app.embeddings.cache import (
    CachedEmbeddingClient,
    build_query_embedding_client,
    cache_key,
)
from app.embeddings.client import EmbeddingClient

DIMS = settings.EMBEDDING_DIMENSIONS


def vec(seed: float) -> list[float]:
    return [seed] * DIMS


class FakeInner:
    model = "fake-model"

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [vec(float(len(t))) for t in texts]


class FakeStore:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.set_calls: list[tuple[str, int | None]] = []

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value
        self.set_calls.append((key, ex))


class BrokenStore:
    def get(self, key):
        raise ConnectionError("redis down")

    def set(self, key, value, ex=None):
        raise ConnectionError("redis down")


def make(store=None, ttl=60):
    inner = FakeInner()
    store = store if store is not None else FakeStore()
    return CachedEmbeddingClient(inner, store, ttl), inner, store


def test_second_call_is_served_from_cache():
    client, inner, _ = make()
    first = client.embed(["hello"])
    second = client.embed(["hello"])
    assert first == second
    assert len(inner.calls) == 1
    assert (client.hits, client.misses) == (1, 1)


def test_partial_hits_only_send_misses_and_preserve_order():
    client, inner, _ = make()
    client.embed(["aa"])
    result = client.embed(["bbb", "aa", "c"])
    assert inner.calls[-1] == ["bbb", "c"]
    assert [v[0] for v in result] == [3.0, 2.0, 1.0]


def test_empty_input_returns_empty_and_skips_everything():
    client, inner, _ = make()
    assert client.embed([]) == []
    assert inner.calls == []


def test_ttl_is_passed_to_the_store():
    client, _, store = make(ttl=123)
    client.embed(["x"])
    assert store.set_calls == [(cache_key("fake-model", "x"), 123)]


def test_key_depends_on_model_and_text():
    assert cache_key("m1", "t") != cache_key("m2", "t")
    assert cache_key("m", "t1") != cache_key("m", "t2")
    assert cache_key("m", "t") == cache_key("m", "t")


def test_fails_open_when_store_raises():
    client, inner, _ = make(store=BrokenStore())
    result = client.embed(["hello"])
    assert result == [vec(5.0)]
    assert len(inner.calls) == 1


def test_corrupt_cached_value_is_a_miss_and_is_refreshed():
    client, inner, store = make()
    store.data[cache_key("fake-model", "hi")] = "not json"
    assert client.embed(["hi"]) == [vec(2.0)]
    assert json.loads(store.data[cache_key("fake-model", "hi")]) == vec(2.0)


def test_wrong_dimension_cached_value_is_a_miss():
    client, inner, store = make()
    store.data[cache_key("fake-model", "hi")] = json.dumps([0.1, 0.2])
    assert client.embed(["hi"]) == [vec(2.0)]
    assert len(inner.calls) == 1


def test_factory_returns_plain_client_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_CACHE_ENABLED", False)
    assert isinstance(build_query_embedding_client(), EmbeddingClient)


def test_factory_returns_cached_client_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://localhost:6379")
    cache_module._redis_store.cache_clear()
    client = build_query_embedding_client()
    assert isinstance(client, CachedEmbeddingClient)
    cache_module._redis_store.cache_clear()


def _real_redis_or_skip():
    import redis

    client = redis.Redis.from_url(
        settings.REDIS_URL, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5
    )
    try:
        client.ping()
    except Exception:
        pytest.skip("no Redis reachable at REDIS_URL")
    return client


def test_real_redis_round_trip_and_ttl():
    client = _real_redis_or_skip()
    text = f"real-redis-test-{uuid.uuid4()}"
    inner = FakeInner()
    cached = CachedEmbeddingClient(inner, client, ttl_seconds=30)
    key = cache_key(inner.model, text)
    try:
        first = cached.embed([text])
        second = cached.embed([text])
        assert first == second
        assert len(inner.calls) == 1
        ttl = client.ttl(key)
        assert 0 < ttl <= 30
    finally:
        client.delete(key)

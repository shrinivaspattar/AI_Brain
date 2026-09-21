"""Suite-wide test isolation from a developer's own .env.

A developer may switch optional features on locally (hybrid search, the
embedding cache). Tests assume the documented defaults, so pin them here;
a test that needs a feature on sets it explicitly with monkeypatch, which
runs after this fixture and wins."""

import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def _pin_optional_features_to_defaults(monkeypatch):
    monkeypatch.setattr(settings, "SEARCH_HYBRID_ENABLED", False)
    monkeypatch.setattr(settings, "EMBEDDING_CACHE_ENABLED", False)

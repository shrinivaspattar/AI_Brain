import httpx
import pytest

from app.services.web_search_service import WebSearchService, WebSearchUnavailableError


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._payload


def test_search_returns_parsed_results(monkeypatch):
    payload = {
        "results": [
            {"title": "Result one", "url": "https://example.com/1", "content": "First snippet"},
            {"title": "Result two", "url": "https://example.com/2", "content": "Second snippet"},
        ]
    }

    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse(payload)

    monkeypatch.setattr(httpx, "get", fake_get)

    service = WebSearchService(base_url="http://localhost:8080")
    results = service.search("python timeout")

    assert captured["url"] == "http://localhost:8080/search"
    assert captured["params"] == {"q": "python timeout", "format": "json"}
    assert [r.title for r in results] == ["Result one", "Result two"]
    assert [r.url for r in results] == ["https://example.com/1", "https://example.com/2"]
    assert [r.snippet for r in results] == ["First snippet", "Second snippet"]


def test_search_truncates_to_max_results(monkeypatch):
    payload = {"results": [{"title": f"R{i}", "url": f"https://x/{i}", "content": ""} for i in range(10)]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(payload))

    service = WebSearchService(base_url="http://localhost:8080")
    results = service.search("q", max_results=3)

    assert len(results) == 3


def test_search_raises_web_search_unavailable_on_connection_error(monkeypatch):
    def fake_get(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", fake_get)

    service = WebSearchService(base_url="http://localhost:8080")
    with pytest.raises(WebSearchUnavailableError):
        service.search("q")


def test_search_raises_web_search_unavailable_on_http_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse({}, status_code=500))

    service = WebSearchService(base_url="http://localhost:8080")
    with pytest.raises(WebSearchUnavailableError):
        service.search("q")


def test_search_base_url_defaults_to_settings(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEARXNG_URL", "http://searxng:8080/")
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _FakeResponse({"results": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    WebSearchService().search("q")

    assert captured["url"] == "http://searxng:8080/search"

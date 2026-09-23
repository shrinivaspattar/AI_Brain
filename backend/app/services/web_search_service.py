from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import settings


class WebSearchUnavailableError(RuntimeError):
    """Raised when the SearXNG instance can't be reached or returns an
    error - e.g. the container isn't running. Never allowed to crash a
    chat turn; ChatService catches it and tells the model no results
    came back rather than pretending a search happened."""


@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str


class WebSearchService:
    """Thin wrapper around a self-hosted SearXNG instance's JSON API - see
    docker/searxng/settings.yml for why that instance enables JSON and
    disables its request limiter. This is the only outbound-to-the-live-
    internet call in the project; callers gate it behind
    settings.WEB_SEARCH_ENABLED plus a per-message opt-in."""

    def __init__(self, base_url: str | None = None, timeout: float = 10.0):
        self.base_url = (base_url or settings.SEARXNG_URL).rstrip("/")
        self.timeout = timeout

    def search(self, query: str, max_results: int | None = None) -> list[WebSearchResult]:
        limit = max_results if max_results is not None else settings.WEB_SEARCH_MAX_RESULTS
        try:
            response = httpx.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001 - httpx/JSON can raise several distinct error types
            raise WebSearchUnavailableError(str(exc)) from exc

        return [
            WebSearchResult(
                title=item.get("title") or "",
                url=item.get("url") or "",
                snippet=item.get("content") or "",
            )
            for item in data.get("results", [])[:limit]
        ]

"""SearXNG search provider — self-hosted metasearch, zero API cost.

The default first-tier provider: a private SearXNG instance means no per-query
cost and works in air-gapped deployments.

Environment variables:
  MANTISFETCH_SEARXNG_URL — instance base URL, e.g. http://searxng:8080 (required)
"""

from __future__ import annotations

import logging
import os

from .base import SearchProvider, SearchResult, _search_http_json

logger = logging.getLogger(__name__)

# SearXNG accepts time_range = day|week|month|year; our freshness enum is a subset.
_FRESHNESS_TIME_RANGE = {"day": "day", "week": "week", "month": "month"}


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class SearxngProvider(SearchProvider):
    """Query a SearXNG instance via its JSON API (``/search?format=json``)."""

    name = "searxng"

    def __init__(self) -> None:
        url = os.environ.get("MANTISFETCH_SEARXNG_URL", "").strip()
        if not url:
            raise RuntimeError(
                "MANTISFETCH_SEARXNG_URL is not set (required for the searxng search provider)."
            )
        self._base_url = url.rstrip("/")

    async def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        lang: str = "en",
        freshness: str | None = None,
    ) -> list[SearchResult]:
        params: dict[str, object] = {
            "q": query,
            "format": "json",
            "language": lang,
            "pageno": 1,
        }
        time_range = _FRESHNESS_TIME_RANGE.get((freshness or "").lower())
        if time_range:
            params["time_range"] = time_range

        data = await _search_http_json(self.name, "GET", f"{self._base_url}/search", params=params)

        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            return []
        out: list[SearchResult] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            out.append(
                SearchResult(
                    url=url,
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("content") or ""),
                    published_at=item.get("publishedDate") or None,
                    score=_as_float(item.get("score")),
                    provider=self.name,
                )
            )
            if len(out) >= max_results:
                break
        return out

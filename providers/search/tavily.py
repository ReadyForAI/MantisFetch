"""Tavily search provider — AI-native search with LLM-friendly snippets.

A quality-upgrade option over the self-hosted SearXNG default.

Environment variables:
  MANTISFETCH_TAVILY_API_KEY — Tavily API key (falls back to MANTISFETCH_SEARCH_API_KEY)
  MANTISFETCH_SEARCH_API_KEY — shared API key used when the per-provider var is unset
  MANTISFETCH_TAVILY_URL     — base URL override (default https://api.tavily.com; test/self-host hook)
"""

from __future__ import annotations

import logging
import os

from .base import SearchProvider, SearchResult, _search_http_json

logger = logging.getLogger(__name__)

# Tavily's `days` filters by recency (int days); map our freshness enum onto it.
_FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 30}


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class TavilyProvider(SearchProvider):
    """Query the Tavily search API (``POST /search``, Bearer auth)."""

    name = "tavily"

    def __init__(self) -> None:
        self._api_key = (
            os.environ.get("MANTISFETCH_TAVILY_API_KEY", "").strip()
            or os.environ.get("MANTISFETCH_SEARCH_API_KEY", "").strip()
        )
        if not self._api_key:
            raise RuntimeError(
                "MANTISFETCH_TAVILY_API_KEY / MANTISFETCH_SEARCH_API_KEY is not set "
                "(required for the tavily search provider)."
            )
        self._base_url = os.environ.get("MANTISFETCH_TAVILY_URL", "https://api.tavily.com").rstrip(
            "/"
        )

    async def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        lang: str = "en",
        freshness: str | None = None,
    ) -> list[SearchResult]:
        body: dict[str, object] = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        days = _FRESHNESS_DAYS.get((freshness or "").lower())
        if days is not None:
            body["days"] = days

        data = await _search_http_json(
            self.name,
            "POST",
            f"{self._base_url}/search",
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

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
                    published_at=item.get("published_date") or None,
                    score=_as_float(item.get("score")),
                    provider=self.name,
                )
            )
            if len(out) >= max_results:
                break
        return out

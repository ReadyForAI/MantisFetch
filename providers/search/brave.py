"""Brave Search provider — independent index with a free tier (contribution-friendly).

Environment variables:
  MANTISFETCH_SEARCH_API_KEY — Brave subscription token (required)
  MANTISFETCH_BRAVE_URL      — base URL override (default https://api.search.brave.com/res/v1; test/self-host hook)

Response shape (GET /web/search): ``web.results[]`` holds ``{title, url, description,
age/page_age, ...}``. Brave returns no numeric relevance score (results are ranked).
"""

from __future__ import annotations

import logging
import os

from .base import SearchProvider, SearchResult, _search_http_json

logger = logging.getLogger(__name__)

# Brave's freshness codes: pd (24h) | pw (7d) | pm (31d) | py (1y).
_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm"}


class BraveProvider(SearchProvider):
    """Query the Brave Web Search API (``GET /web/search``, X-Subscription-Token)."""

    name = "brave"

    def __init__(self) -> None:
        self._api_key = os.environ.get("MANTISFETCH_SEARCH_API_KEY", "").strip()
        if not self._api_key:
            raise RuntimeError(
                "MANTISFETCH_SEARCH_API_KEY is not set (required for the brave search provider)."
            )
        self._base_url = os.environ.get(
            "MANTISFETCH_BRAVE_URL", "https://api.search.brave.com/res/v1"
        ).rstrip("/")

    async def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        lang: str = "en",
        freshness: str | None = None,
    ) -> list[SearchResult]:
        # Brave's search_lang wants a bare language subtag ("en"), not a locale tag
        # ("en-US" — the app's DEFAULT_LANG); a locale tag is rejected as a 400.
        search_lang = (lang or "en").replace("_", "-").split("-")[0].strip().lower() or "en"
        params: dict[str, object] = {"q": query, "count": max_results, "search_lang": search_lang}
        fr = _FRESHNESS.get((freshness or "").lower())
        if fr:
            params["freshness"] = fr

        data = await _search_http_json(
            self.name,
            "GET",
            f"{self._base_url}/web/search",
            params=params,
            headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
        )

        web = data.get("web") if isinstance(data.get("web"), dict) else {}
        raw_results = web.get("results")
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
                    snippet=str(item.get("description") or ""),
                    published_at=item.get("page_age") or item.get("age") or None,
                    score=None,  # Brave ranks results but returns no numeric score
                    provider=self.name,
                )
            )
            if len(out) >= max_results:
                break
        return out

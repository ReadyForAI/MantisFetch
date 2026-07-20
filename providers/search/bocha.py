"""Bocha (博查) search provider — China-compliant search.

Environment variables:
  MANTISFETCH_BOCHA_API_KEY  — Bocha API key (falls back to MANTISFETCH_SEARCH_API_KEY)
  MANTISFETCH_SEARCH_API_KEY — shared API key used when the per-provider var is unset
  MANTISFETCH_BOCHA_URL      — base URL override (default https://api.bochaai.com; test/self-host hook)

Response shape (POST /v1/web-search): a top-level envelope ``{code, msg, data}``
whose ``data.webPages.value[]`` holds ``{name, url, snippet, datePublished, ...}``.
Bocha does not return a numeric relevance score.
"""

from __future__ import annotations

import logging
import os

from .base import (
    SearchProvider,
    SearchProviderUnavailable,
    SearchResult,
    _raise_for_search_status,
    _search_http_json,
)

logger = logging.getLogger(__name__)

# Bocha's freshness enum: oneDay | oneWeek | oneMonth | oneYear | noLimit.
_FRESHNESS = {"day": "oneDay", "week": "oneWeek", "month": "oneMonth"}


class BochaProvider(SearchProvider):
    """Query the Bocha web-search API (``POST /v1/web-search``, Bearer auth)."""

    name = "bocha"

    def __init__(self) -> None:
        self._api_key = (
            os.environ.get("MANTISFETCH_BOCHA_API_KEY", "").strip()
            or os.environ.get("MANTISFETCH_SEARCH_API_KEY", "").strip()
        )
        if not self._api_key:
            raise RuntimeError(
                "MANTISFETCH_BOCHA_API_KEY / MANTISFETCH_SEARCH_API_KEY is not set "
                "(required for the bocha search provider)."
            )
        self._base_url = os.environ.get("MANTISFETCH_BOCHA_URL", "https://api.bochaai.com").rstrip(
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
        body: dict[str, object] = {"query": query, "count": max_results}
        fr = _FRESHNESS.get((freshness or "").lower())
        if fr:
            body["freshness"] = fr

        data = await _search_http_json(
            self.name,
            "POST",
            f"{self._base_url}/v1/web-search",
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

        # Bocha signals application errors (bad key, quota, rate limit) in the
        # envelope `code` while the HTTP status stays 200 — map a non-200 code onto
        # the error contract (5xx → unavailable/retriable, 4xx → config) instead of
        # silently returning [] and masking the failure as an empty result.
        code = data.get("code")
        if code is not None and code != 200:
            try:
                status = int(code)
            except (TypeError, ValueError):
                status = 502
            msg = str(data.get("msg") or "")
            if status >= 400:
                _raise_for_search_status(self.name, status, msg)  # 4xx config / 5xx unavailable
            # Any other non-200 code is still an error, not an empty result —
            # never fall through and mask it as [].
            raise SearchProviderUnavailable(f"{self.name}: non-success code {code}: {msg}")

        # Results live under data.webPages.value; tolerate the envelope being absent.
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        web_pages = payload.get("webPages") if isinstance(payload.get("webPages"), dict) else {}
        raw_results = web_pages.get("value")
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
                    title=str(item.get("name") or ""),
                    snippet=str(item.get("snippet") or ""),
                    published_at=item.get("datePublished") or None,
                    score=None,  # Bocha returns no relevance score
                    provider=self.name,
                )
            )
            if len(out) >= max_results:
                break
        return out

"""Abstract base for web-search providers.

Mirrors the LLM Provider pattern (``providers/base.py``) but async: search
backends do network I/O and are called from the async browser service. A search
returns a list of :class:`SearchResult`.

There is intentionally **no** ``health()`` preflight — fallback is decided per
call from the actual exception, not a probe (see ``providers/search/__init__``).

Error contract (drives the fallback chain):
  - return ``[]`` for a valid empty result — do NOT raise;
  - raise :class:`SearchProviderUnavailable` on connection error / 5xx / timeout
    (retriable — the chain moves to the next provider);
  - raise :class:`SearchConfigError` on 4xx (bad key / quota / bad request —
    surfaced to the operator, NOT masked by falling back).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Per-provider request budget. A provider that does not answer within this many
# seconds counts as unavailable and the chain falls back to the next one.
SEARCH_TIMEOUT_SEC = 10.0


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One search hit. ``snippet``/``title`` are attacker-controllable content
    (SEO-poisoned pages) — wrap them at the injection boundary before display."""

    url: str
    title: str
    snippet: str
    published_at: str | None  # ISO8601 when the provider supplies it
    score: float | None  # provider-native relevance, else None
    provider: str  # "searxng" / "tavily" / ...


class SearchError(Exception):
    """Base class for search-provider failures."""


class SearchProviderUnavailable(SearchError):
    """Retriable provider-level failure (connection error, 5xx, timeout).

    Triggers fallback to the next provider in the chain.
    """


class SearchConfigError(SearchError):
    """Non-retriable configuration failure (HTTP 4xx: bad key, quota, bad request).

    Does NOT trigger fallback — surfaced so an operator fixes it, instead of the
    error being masked by silent degradation to another provider.
    """


class UnknownSearchProviderError(SearchError):
    """A per-request ``provider`` selection outside the addressable set
    (MANTISFETCH_SEARCH_PROVIDER + fallback chain + MANTISFETCH_SEARCH_PROVIDERS).

    A caller error (mapped to HTTP 400), distinct from a misconfigured provider
    (RuntimeError → 502) or a runtime provider failure (SearchError → 502).
    """


def _raise_for_search_status(provider: str, status_code: int, body: str = "") -> None:
    """Map an HTTP status to the fallback contract. 5xx → retriable, 4xx → config."""
    if status_code >= 500:
        raise SearchProviderUnavailable(f"{provider}: HTTP {status_code}")
    if status_code >= 400:
        raise SearchConfigError(f"{provider}: HTTP {status_code}: {body[:200]}")


async def _search_http_json(provider: str, method: str, url: str, **kwargs) -> dict:
    """Run one search HTTP request and return the parsed JSON object.

    Translates transport/timeout errors and 5xx into :class:`SearchProviderUnavailable`,
    4xx into :class:`SearchConfigError`. Malformed/non-object JSON is treated as a
    retriable upstream fault.
    """
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_SEC) as client:
            resp = await client.request(method, url, **kwargs)
    except httpx.TransportError as exc:  # connect errors, timeouts, network faults
        raise SearchProviderUnavailable(f"{provider}: transport error: {exc!r}") from exc

    _raise_for_search_status(provider, resp.status_code, resp.text)

    try:
        data = resp.json()
    except ValueError as exc:
        raise SearchProviderUnavailable(f"{provider}: malformed JSON response: {exc!r}") from exc
    if not isinstance(data, dict):
        raise SearchProviderUnavailable(f"{provider}: unexpected JSON shape (not an object)")
    return data


class SearchProvider(ABC):
    """Unified async interface for web-search backends."""

    name: str

    @property
    def throttle_keys(self) -> tuple[str, ...]:
        """Backend bucket keys for the process-level min-interval throttle. A single
        provider charges its own bucket; a fallback chain overrides this to charge
        *every* member it might query, so neither the primary-share nor the
        failover path can bypass the interval for a backend an explicit
        ``provider=<name>`` request would also hit."""
        return (self.name,)

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        lang: str = "en",
        freshness: str | None = None,  # "day" | "week" | "month" | None
    ) -> list[SearchResult]:
        """Run one search.

        Return ``[]`` for a valid empty result (do not raise). Raise
        :class:`SearchProviderUnavailable` on connection error / 5xx / timeout,
        :class:`SearchConfigError` on 4xx.
        """

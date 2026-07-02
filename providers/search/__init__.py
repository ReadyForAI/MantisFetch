"""Web-search provider factory + fallback chain.

Selected via ``MANTISFETCH_SEARCH_PROVIDER`` (unset ⇒ search is disabled: the
``/web/search*`` endpoints 404 and the MCP search tools are not registered).
Mirrors the LLM provider factory (``providers/__init__.py``) but adds a fallback
chain — the LLM side has no fallback, so this behaviour is new here.

Fallback contract: try providers in order; move to the next ONLY on
:class:`SearchProviderUnavailable` (connection error / 5xx / 10s timeout). A
:class:`SearchConfigError` (4xx) or an empty result stops the chain — the former
surfaces to the operator, the latter is a valid answer.

Environment variables:
  MANTISFETCH_SEARCH_PROVIDER          primary provider; unset ⇒ disabled
  MANTISFETCH_SEARCH_FALLBACK          comma-separated fallback chain, e.g. "tavily,searxng"
  MANTISFETCH_SEARXNG_URL              SearXNG instance URL
  MANTISFETCH_SEARCH_API_KEY           Tavily / bocha / brave API key
  MANTISFETCH_SEARCH_MAX_RESULTS       default result cap (default 10, hard max 20)
  MANTISFETCH_SEARCH_MIN_INTERVAL_SEC  min seconds between searches (default 2)
"""

from __future__ import annotations

import logging
import os

from .base import (
    SearchConfigError,
    SearchError,
    SearchProvider,
    SearchProviderUnavailable,
    SearchResult,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SearchConfigError",
    "SearchError",
    "SearchProvider",
    "SearchProviderUnavailable",
    "SearchResult",
    "create_search_provider",
    "search_enabled",
    "default_max_results",
    "clamp_max_results",
    "min_interval_sec",
    "HARD_MAX_RESULTS",
]

HARD_MAX_RESULTS = 20
_DEFAULT_MAX_RESULTS = 10
_DEFAULT_MIN_INTERVAL_SEC = 2.0


def _registry() -> dict[str, type[SearchProvider]]:
    """Name → provider class. Lazy import so the heavy provider modules load only
    when search is actually configured."""
    from .bocha import BochaProvider
    from .brave import BraveProvider
    from .searxng import SearxngProvider
    from .tavily import TavilyProvider

    return {
        "searxng": SearxngProvider,
        "tavily": TavilyProvider,
        "bocha": BochaProvider,
        "brave": BraveProvider,
    }


# ── configuration helpers ──────────────────────────────────────────────────────


def search_enabled() -> bool:
    """True when a primary search provider is configured."""
    return bool(os.environ.get("MANTISFETCH_SEARCH_PROVIDER", "").strip())


def clamp_max_results(n: int) -> int:
    """Clamp a requested result count into [1, HARD_MAX_RESULTS]."""
    try:
        value = int(n)
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_RESULTS
    return max(1, min(value, HARD_MAX_RESULTS))


def default_max_results() -> int:
    """The configured default result cap (MANTISFETCH_SEARCH_MAX_RESULTS)."""
    raw = os.environ.get("MANTISFETCH_SEARCH_MAX_RESULTS", str(_DEFAULT_MAX_RESULTS))
    try:
        value = int(raw)
    except ValueError:
        value = _DEFAULT_MAX_RESULTS
    return clamp_max_results(value)


def min_interval_sec() -> float:
    """Minimum seconds between searches (process-level throttle)."""
    raw = os.environ.get("MANTISFETCH_SEARCH_MIN_INTERVAL_SEC", str(_DEFAULT_MIN_INTERVAL_SEC))
    try:
        value = float(raw)
    except ValueError:
        value = _DEFAULT_MIN_INTERVAL_SEC
    return max(0.0, value)


# ── factory + fallback ─────────────────────────────────────────────────────────


def _chain_names() -> list[str]:
    """Ordered, de-duplicated provider names: primary then the fallback chain."""
    primary = os.environ.get("MANTISFETCH_SEARCH_PROVIDER", "").strip().lower()
    if not primary:
        return []
    names = [primary]
    for part in os.environ.get("MANTISFETCH_SEARCH_FALLBACK", "").split(","):
        name = part.strip().lower()
        if name and name not in names:
            names.append(name)
    return names


def _build(name: str) -> SearchProvider:
    cls = _registry().get(name)
    if cls is None:
        raise ValueError(f"Unknown search provider: {name!r}. Supported: {sorted(_registry())}")
    return cls()  # raises RuntimeError on misconfiguration (missing URL / key)


def create_search_provider() -> SearchProvider | None:
    """Build the active provider (a fallback chain if more than one is configured).

    Returns ``None`` when search is disabled (no primary provider set)."""
    names = _chain_names()
    if not names:
        return None
    providers = [_build(name) for name in names]
    if len(providers) == 1:
        return providers[0]
    return _FallbackSearchProvider(providers)


class _FallbackSearchProvider(SearchProvider):
    """Try each provider in order; fall through only on retriable unavailability."""

    def __init__(self, providers: list[SearchProvider]) -> None:
        self._providers = providers
        self.name = "+".join(p.name for p in providers)

    async def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        lang: str = "en",
        freshness: str | None = None,
    ) -> list[SearchResult]:
        last_exc: SearchProviderUnavailable | None = None
        for provider in self._providers:
            try:
                return await provider.search(
                    query, max_results=max_results, lang=lang, freshness=freshness
                )
            except SearchProviderUnavailable as exc:
                # retriable — try the next provider
                last_exc = exc
                logger.warning(
                    "search provider %s unavailable, falling back: %s", provider.name, exc
                )
                continue
            # SearchConfigError propagates immediately (no fallback); an empty
            # list is a valid return and also stops here.
        raise SearchProviderUnavailable(
            f"all search providers exhausted ({self.name})"
        ) from last_exc

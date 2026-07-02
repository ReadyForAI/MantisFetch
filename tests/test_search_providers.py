"""T1 — SearchProvider abstraction, config, and fallback-chain semantics."""

from unittest.mock import patch

import pytest

from providers.search import (
    HARD_MAX_RESULTS,
    _FallbackSearchProvider,
    clamp_max_results,
    create_search_provider,
    default_max_results,
    min_interval_sec,
    search_enabled,
)
from providers.search.base import (
    SearchConfigError,
    SearchProvider,
    SearchProviderUnavailable,
    SearchResult,
    _raise_for_search_status,
)
from providers.search.searxng import SearxngProvider
from providers.search.tavily import TavilyProvider

# ── all search env vars, cleared between tests ─────────────────────────────────
_SEARCH_ENV = [
    "MANTISFETCH_SEARCH_PROVIDER",
    "MANTISFETCH_SEARCH_FALLBACK",
    "MANTISFETCH_SEARXNG_URL",
    "MANTISFETCH_SEARCH_API_KEY",
    "MANTISFETCH_TAVILY_URL",
    "MANTISFETCH_SEARCH_MAX_RESULTS",
    "MANTISFETCH_SEARCH_MIN_INTERVAL_SEC",
]


@pytest.fixture(autouse=True)
def _clean_search_env(monkeypatch):
    for name in _SEARCH_ENV:
        monkeypatch.delenv(name, raising=False)


# ── a fake provider for fallback tests ─────────────────────────────────────────
class _FakeProvider(SearchProvider):
    def __init__(self, name, *, raises=None, results=None):
        self.name = name
        self._raises = raises
        self._results = results if results is not None else []
        self.calls = 0

    async def search(self, query, *, max_results=10, lang="en", freshness=None):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._results


def _hit(url="https://ex.com", provider="searxng"):
    return SearchResult(
        url=url, title="t", snippet="s", published_at=None, score=None, provider=provider
    )


# ── config helpers ─────────────────────────────────────────────────────────────
def test_clamp_max_results_bounds():
    assert clamp_max_results(999) == HARD_MAX_RESULTS
    assert clamp_max_results(0) == 1
    assert clamp_max_results(-5) == 1
    assert clamp_max_results(8) == 8
    assert clamp_max_results("bad") == 10  # non-int falls back to default, then clamped


def test_default_max_results(monkeypatch):
    assert default_max_results() == 10
    monkeypatch.setenv("MANTISFETCH_SEARCH_MAX_RESULTS", "5")
    assert default_max_results() == 5
    monkeypatch.setenv("MANTISFETCH_SEARCH_MAX_RESULTS", "999")
    assert default_max_results() == HARD_MAX_RESULTS
    monkeypatch.setenv("MANTISFETCH_SEARCH_MAX_RESULTS", "junk")
    assert default_max_results() == 10


def test_min_interval_sec(monkeypatch):
    assert min_interval_sec() == 2.0
    monkeypatch.setenv("MANTISFETCH_SEARCH_MIN_INTERVAL_SEC", "0.5")
    assert min_interval_sec() == 0.5
    monkeypatch.setenv("MANTISFETCH_SEARCH_MIN_INTERVAL_SEC", "-3")
    assert min_interval_sec() == 0.0
    monkeypatch.setenv("MANTISFETCH_SEARCH_MIN_INTERVAL_SEC", "nan?")
    assert min_interval_sec() == 2.0


def test_search_enabled(monkeypatch):
    assert search_enabled() is False
    monkeypatch.setenv("MANTISFETCH_SEARCH_PROVIDER", "searxng")
    assert search_enabled() is True


# ── factory ────────────────────────────────────────────────────────────────────
def test_create_disabled_returns_none():
    assert create_search_provider() is None


def test_create_single_provider(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("MANTISFETCH_SEARXNG_URL", "http://searxng:8080")
    provider = create_search_provider()
    assert isinstance(provider, SearxngProvider)


def test_create_fallback_chain(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("MANTISFETCH_SEARCH_FALLBACK", "tavily,searxng")  # dup dropped
    monkeypatch.setenv("MANTISFETCH_SEARXNG_URL", "http://searxng:8080")
    monkeypatch.setenv("MANTISFETCH_SEARCH_API_KEY", "tvly-x")
    provider = create_search_provider()
    assert isinstance(provider, _FallbackSearchProvider)
    assert provider.name == "searxng+tavily"


def test_create_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARCH_PROVIDER", "nope")
    with pytest.raises(ValueError, match="Unknown search provider"):
        create_search_provider()


def test_searxng_missing_url_raises(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARCH_PROVIDER", "searxng")
    with pytest.raises(RuntimeError, match="MANTISFETCH_SEARXNG_URL"):
        create_search_provider()


# ── status mapping (護栏 2: 5xx retriable, 4xx config) ──────────────────────────
def test_raise_for_status_5xx_unavailable():
    with pytest.raises(SearchProviderUnavailable):
        _raise_for_search_status("p", 503)


def test_raise_for_status_4xx_config():
    with pytest.raises(SearchConfigError):
        _raise_for_search_status("p", 401, "unauthorized")


def test_raise_for_status_2xx_ok():
    _raise_for_search_status("p", 200)  # no raise


# ── provider parsing (patch the HTTP seam at the use site) ──────────────────────
async def test_searxng_parse(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARXNG_URL", "http://searxng:8080")

    async def fake_json(provider, method, url, **kwargs):
        assert kwargs["params"]["format"] == "json"
        return {
            "results": [
                {
                    "url": "https://a.com",
                    "title": "A",
                    "content": "snip a",
                    "publishedDate": "2026-01-01",
                    "score": 0.9,
                },
                {"title": "no-url skipped", "content": "x"},
                {"url": "https://b.com", "title": "B", "content": "snip b"},
            ]
        }

    with patch("providers.search.searxng._search_http_json", fake_json):
        results = await SearxngProvider().search("q", max_results=10)

    assert [r.url for r in results] == ["https://a.com", "https://b.com"]  # no-url dropped
    assert results[0].snippet == "snip a"
    assert results[0].published_at == "2026-01-01"
    assert results[0].score == 0.9
    assert results[1].score is None
    assert all(r.provider == "searxng" for r in results)


async def test_searxng_respects_max_results(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARXNG_URL", "http://searxng:8080")

    async def fake_json(provider, method, url, **kwargs):
        return {
            "results": [
                {"url": f"https://{i}.com", "title": "t", "content": "c"} for i in range(10)
            ]
        }

    with patch("providers.search.searxng._search_http_json", fake_json):
        results = await SearxngProvider().search("q", max_results=3)
    assert len(results) == 3


async def test_searxng_freshness_maps_time_range(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARXNG_URL", "http://searxng:8080")
    seen = {}

    async def fake_json(provider, method, url, **kwargs):
        seen.update(kwargs["params"])
        return {"results": []}

    with patch("providers.search.searxng._search_http_json", fake_json):
        await SearxngProvider().search("q", freshness="week")
    assert seen["time_range"] == "week"


async def test_tavily_parse(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARCH_API_KEY", "tvly-x")

    async def fake_json(provider, method, url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer tvly-x"
        assert kwargs["json"]["days"] == 1  # freshness=day
        return {
            "results": [
                {
                    "url": "https://a.com",
                    "title": "A",
                    "content": "c",
                    "score": 0.5,
                    "published_date": "2026-02-02",
                }
            ]
        }

    with patch("providers.search.tavily._search_http_json", fake_json):
        results = await TavilyProvider().search("q", freshness="day")
    assert results[0].url == "https://a.com"
    assert results[0].snippet == "c"
    assert results[0].published_at == "2026-02-02"
    assert results[0].provider == "tavily"


async def test_provider_empty_results(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARXNG_URL", "http://searxng:8080")

    async def fake_json(provider, method, url, **kwargs):
        return {"results": []}

    with patch("providers.search.searxng._search_http_json", fake_json):
        assert await SearxngProvider().search("q") == []


# ── fallback semantics (護栏 2) ─────────────────────────────────────────────────
async def test_fallback_switches_on_unavailable():
    p1 = _FakeProvider("p1", raises=SearchProviderUnavailable("down"))
    p2 = _FakeProvider("p2", results=[_hit(provider="p2")])
    chain = _FallbackSearchProvider([p1, p2])
    results = await chain.search("q")
    assert results[0].provider == "p2"
    assert p1.calls == 1 and p2.calls == 1


async def test_fallback_does_not_switch_on_config_error():
    p1 = _FakeProvider("p1", raises=SearchConfigError("bad key"))
    p2 = _FakeProvider("p2", results=[_hit()])
    chain = _FallbackSearchProvider([p1, p2])
    with pytest.raises(SearchConfigError):
        await chain.search("q")
    assert p2.calls == 0  # never reached — config errors do not fall back


async def test_fallback_does_not_switch_on_empty():
    p1 = _FakeProvider("p1", results=[])  # valid empty answer
    p2 = _FakeProvider("p2", results=[_hit()])
    chain = _FallbackSearchProvider([p1, p2])
    assert await chain.search("q") == []
    assert p2.calls == 0  # empty is a valid result, not a fallback trigger


async def test_fallback_all_unavailable_raises():
    p1 = _FakeProvider("p1", raises=SearchProviderUnavailable("down1"))
    p2 = _FakeProvider("p2", raises=SearchProviderUnavailable("down2"))
    chain = _FallbackSearchProvider([p1, p2])
    with pytest.raises(SearchProviderUnavailable, match="exhausted"):
        await chain.search("q")
    assert p1.calls == 1 and p2.calls == 1

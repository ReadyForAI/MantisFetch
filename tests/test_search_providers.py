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
from providers.search.bocha import BochaProvider
from providers.search.brave import BraveProvider
from providers.search.searxng import SearxngProvider
from providers.search.tavily import TavilyProvider

# ── all search env vars, cleared between tests ─────────────────────────────────
_SEARCH_ENV = [
    "MANTISFETCH_SEARCH_PROVIDER",
    "MANTISFETCH_SEARCH_FALLBACK",
    "MANTISFETCH_SEARXNG_URL",
    "MANTISFETCH_SEARCH_API_KEY",
    "MANTISFETCH_TAVILY_API_KEY",
    "MANTISFETCH_BOCHA_API_KEY",
    "MANTISFETCH_BRAVE_API_KEY",
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


# ── bocha + brave (T5) ──────────────────────────────────────────────────────────
def test_create_bocha_and_brave(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARCH_API_KEY", "key-x")
    monkeypatch.setenv("MANTISFETCH_SEARCH_PROVIDER", "bocha")
    assert isinstance(create_search_provider(), BochaProvider)
    monkeypatch.setenv("MANTISFETCH_SEARCH_PROVIDER", "brave")
    assert isinstance(create_search_provider(), BraveProvider)


def test_bocha_missing_key_raises(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARCH_PROVIDER", "bocha")
    with pytest.raises(RuntimeError, match="MANTISFETCH_SEARCH_API_KEY"):
        create_search_provider()


# ── per-provider API keys: each API provider prefers its own key and falls back
#    to the shared MANTISFETCH_SEARCH_API_KEY when the per-provider var is unset ──
def test_per_provider_key_used_when_set(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_TAVILY_API_KEY", "tvly-own")
    monkeypatch.setenv("MANTISFETCH_BOCHA_API_KEY", "bocha-own")
    monkeypatch.setenv("MANTISFETCH_BRAVE_API_KEY", "brave-own")
    assert TavilyProvider()._api_key == "tvly-own"
    assert BochaProvider()._api_key == "bocha-own"
    assert BraveProvider()._api_key == "brave-own"


def test_per_provider_key_overrides_shared(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARCH_API_KEY", "shared")
    monkeypatch.setenv("MANTISFETCH_TAVILY_API_KEY", "tvly-own")
    assert TavilyProvider()._api_key == "tvly-own"  # per-provider wins
    assert BochaProvider()._api_key == "shared"  # unset per-provider → shared fallback


def test_shared_key_is_fallback_for_all(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARCH_API_KEY", "shared")
    assert TavilyProvider()._api_key == "shared"
    assert BochaProvider()._api_key == "shared"
    assert BraveProvider()._api_key == "shared"


def test_two_api_providers_hold_distinct_keys(monkeypatch):
    # Motivating case: bocha (CN) and tavily (EN) each with their own key.
    monkeypatch.setenv("MANTISFETCH_BOCHA_API_KEY", "bocha-cn")
    monkeypatch.setenv("MANTISFETCH_TAVILY_API_KEY", "tvly-en")
    assert BochaProvider()._api_key == "bocha-cn"
    assert TavilyProvider()._api_key == "tvly-en"


async def test_bocha_parse(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARCH_API_KEY", "key-x")

    async def fake_json(provider, method, url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer key-x"
        assert kwargs["json"]["freshness"] == "oneMonth"  # freshness=month
        return {
            "code": 200,
            "data": {
                "webPages": {
                    "value": [
                        {
                            "name": "N A",
                            "url": "https://a.com",
                            "snippet": "s a",
                            "datePublished": "2026-03-03",
                        },
                        {"name": "no-url", "snippet": "x"},
                    ]
                }
            },
        }

    with patch("providers.search.bocha._search_http_json", fake_json):
        results = await BochaProvider().search("q", freshness="month")
    assert [r.url for r in results] == ["https://a.com"]  # no-url dropped
    assert results[0].title == "N A"
    assert results[0].snippet == "s a"
    assert results[0].published_at == "2026-03-03"
    assert results[0].score is None
    assert results[0].provider == "bocha"


async def test_brave_parse(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_SEARCH_API_KEY", "key-x")

    async def fake_json(provider, method, url, **kwargs):
        assert kwargs["headers"]["X-Subscription-Token"] == "key-x"
        assert kwargs["params"]["freshness"] == "pw"  # freshness=week
        assert kwargs["params"]["search_lang"] == "en"  # en-US locale → en subtag
        return {
            "web": {
                "results": [
                    {
                        "title": "T A",
                        "url": "https://a.com",
                        "description": "d a",
                        "page_age": "2026-04-04",
                    },
                ]
            }
        }

    with patch("providers.search.brave._search_http_json", fake_json):
        results = await BraveProvider().search("q", lang="en-US", freshness="week")
    assert results[0].url == "https://a.com"
    assert results[0].title == "T A"
    assert results[0].snippet == "d a"  # description → snippet
    assert results[0].published_at == "2026-04-04"
    assert results[0].provider == "brave"


async def test_bocha_error_envelope_raises(monkeypatch):
    """Bocha returns HTTP 200 with an error `code`; that must raise (config for 4xx,
    unavailable for 5xx), not be masked as an empty result."""
    monkeypatch.setenv("MANTISFETCH_SEARCH_API_KEY", "key-x")

    async def fake_403(provider, method, url, **kwargs):
        return {"code": 403, "msg": "forbidden", "data": {}}

    with patch("providers.search.bocha._search_http_json", fake_403):
        with pytest.raises(SearchConfigError):
            await BochaProvider().search("q")

    async def fake_500(provider, method, url, **kwargs):
        return {"code": 500, "msg": "internal error"}

    with patch("providers.search.bocha._search_http_json", fake_500):
        with pytest.raises(SearchProviderUnavailable):
            await BochaProvider().search("q")

    # a non-200 code below 400 is still an error, never an empty result
    async def fake_302(provider, method, url, **kwargs):
        return {"code": 302, "msg": "unexpected"}

    with patch("providers.search.bocha._search_http_json", fake_302):
        with pytest.raises(SearchProviderUnavailable):
            await BochaProvider().search("q")

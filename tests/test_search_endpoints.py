"""T2 — /web/search and /web/search_and_capture endpoints + min-interval throttle."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from mantisfetch_browser.models import CaptureResponse
from starlette.testclient import TestClient

from providers.search.base import (
    SearchConfigError,
    SearchProviderUnavailable,
    SearchResult,
)


@pytest.fixture(autouse=True)
def _reset_search_state(monkeypatch):
    """Reset the process-level throttle timestamp and disable the min-interval
    throttle by default so ordering between tests can't leak a spurious 429."""
    import mantisfetch_browser as lb

    lb._last_search_monotonic = 0.0
    monkeypatch.setenv("MANTISFETCH_SEARCH_MIN_INTERVAL_SEC", "0")
    yield


class _FakeSearchProvider:
    name = "fake"

    def __init__(self, results=None, raises=None):
        self._results = results or []
        self._raises = raises

    async def search(self, query, *, max_results=10, lang="en", freshness=None):
        if self._raises is not None:
            raise self._raises
        return self._results[:max_results]


def _sr(url: str) -> SearchResult:
    return SearchResult(
        url=url, title=f"T {url}", snippet="snip", published_at=None, score=0.5, provider="fake"
    )


# ── /web/search ────────────────────────────────────────────────────────────────
def test_search_disabled_returns_404(client: TestClient) -> None:
    with patch("mantisfetch_browser.create_search_provider", return_value=None):
        resp = client.post("/web/search", json={"query": "q"})
    assert resp.status_code == 404


def test_search_returns_results(client: TestClient) -> None:
    provider = _FakeSearchProvider(results=[_sr("https://a.com"), _sr("https://b.com")])
    with patch("mantisfetch_browser.create_search_provider", return_value=provider):
        resp = client.post("/web/search", json={"query": "q", "max_results": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "fake"
    assert [r["url"] for r in data["results"]] == ["https://a.com", "https://b.com"]
    assert data["results"][0]["snippet"] == "snip"
    assert data["searched_at"].endswith("Z")


def test_search_provider_unavailable_502(client: TestClient) -> None:
    provider = _FakeSearchProvider(raises=SearchProviderUnavailable("all exhausted"))
    with patch("mantisfetch_browser.create_search_provider", return_value=provider):
        resp = client.post("/web/search", json={"query": "q"})
    assert resp.status_code == 502


def test_search_config_error_502(client: TestClient) -> None:
    provider = _FakeSearchProvider(raises=SearchConfigError("bad key"))
    with patch("mantisfetch_browser.create_search_provider", return_value=provider):
        resp = client.post("/web/search", json={"query": "q"})
    assert resp.status_code == 502


def test_search_throttle_429(client: TestClient, monkeypatch) -> None:
    provider = _FakeSearchProvider(results=[])
    monkeypatch.setenv("MANTISFETCH_SEARCH_MIN_INTERVAL_SEC", "100")
    with patch("mantisfetch_browser.create_search_provider", return_value=provider):
        r1 = client.post("/web/search", json={"query": "q"})
        r2 = client.post("/web/search", json={"query": "q"})
    assert r1.status_code == 200
    assert r2.status_code == 429  # bare 429 — no Retry-After
    assert "retry-after" not in {k.lower() for k in r2.headers}


# ── /web/search_and_capture ─────────────────────────────────────────────────────
def test_search_and_capture_serial_with_skip(client: TestClient) -> None:
    provider = _FakeSearchProvider(results=[_sr("https://a.com"), _sr("https://b.com")])
    seen_reqs = []
    seen_ttls = []

    async def fake_capture(req, *, url_ttl_hours=None):
        seen_reqs.append(req)
        seen_ttls.append(url_ttl_hours)
        if req.url == "https://b.com":
            raise HTTPException(502, "goto timeout")
        return CaptureResponse(doc_id="WEB-100", digest="dg", section_count=2, table_count=0)

    with (
        patch("mantisfetch_browser.create_search_provider", return_value=provider),
        patch("mantisfetch_browser._capture_impl", new=fake_capture),
    ):
        resp = client.post(
            "/web/search_and_capture",
            json={"query": "q", "capture_top": 2, "tags": ["research"]},
        )
    assert resp.status_code == 200
    data = resp.json()
    # one captured, one skipped — a single failure does not abort the batch
    assert [c["rank"] for c in data["captured"]] == [1]
    assert data["captured"][0]["doc_id"] == "WEB-100"
    assert [s["rank"] for s in data["skipped"]] == [2]
    assert "goto timeout" in data["skipped"][0]["reason"]
    # search provenance is stamped into the capture metadata (rank-specific)
    assert seen_reqs[0].metadata["source"] == "web_search"
    assert seen_reqs[0].metadata["search_query"] == "q"
    assert seen_reqs[0].metadata["search_provider"] == "fake"
    assert seen_reqs[0].metadata["search_rank"] == 1
    assert seen_reqs[0].tags == ["research"]
    # B5: search_and_capture applies its own URL TTL (default 24h)
    assert all(t is not None and t >= 24.0 for t in seen_ttls)


def test_search_and_capture_caps_top_at_3(client: TestClient) -> None:
    provider = _FakeSearchProvider(results=[_sr(f"https://{i}.com") for i in range(10)])
    calls = []

    async def fake_capture(req, *, url_ttl_hours=None):
        calls.append(req.url)
        return CaptureResponse(doc_id="WEB-1", digest="d", section_count=1, table_count=0)

    with (
        patch("mantisfetch_browser.create_search_provider", return_value=provider),
        patch("mantisfetch_browser._capture_impl", new=fake_capture),
    ):
        resp = client.post("/web/search_and_capture", json={"query": "q", "capture_top": 9})
    assert resp.status_code == 200
    assert len(calls) == 3  # capped at SEARCH_CAPTURE_TOP_MAX regardless of request


def test_search_and_capture_disabled_404(client: TestClient) -> None:
    with patch("mantisfetch_browser.create_search_provider", return_value=None):
        resp = client.post("/web/search_and_capture", json={"query": "q"})
    assert resp.status_code == 404


def test_search_and_capture_bad_content_type_422(client: TestClient) -> None:
    provider = _FakeSearchProvider(results=[_sr("https://a.com")])
    with patch("mantisfetch_browser.create_search_provider", return_value=provider):
        resp = client.post("/web/search_and_capture", json={"query": "q", "content_type": "Nope"})
    assert resp.status_code == 422


def test_search_provider_misconfigured_502(client: TestClient, monkeypatch) -> None:
    """A configured provider that raises during construction (searxng without a URL)
    surfaces as 502, not an unhandled 500. Uses the real factory (no patch)."""
    monkeypatch.setenv("MANTISFETCH_SEARCH_PROVIDER", "searxng")
    monkeypatch.delenv("MANTISFETCH_SEARXNG_URL", raising=False)
    resp = client.post("/web/search", json={"query": "q"})
    assert resp.status_code == 502
    assert "misconfigured" in resp.json()["detail"]

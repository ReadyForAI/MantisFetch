"""A fallback member is charged when the chain actually queries it (#187).

The min-interval throttle reserved every member of a fallback chain once, up
front. A primary that hung longer than the interval let the fallback's
reservation expire before the chain reached it; a concurrent explicit request
for that backend then went through, and the chain hit the same backend
moments later — 0.02 s apart against a 0.05 s interval, in the report.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import httpx
import mantisfetch_browser as lb
import pytest

from providers.search import _FallbackSearchProvider
from providers.search.base import SearchProvider, SearchProviderUnavailable, SearchResult

INTERVAL = 0.2


@pytest.fixture(autouse=True)
def _throttle(monkeypatch):
    lb._next_search_allowed = {}
    monkeypatch.setenv("MANTISFETCH_SEARCH_MIN_INTERVAL_SEC", str(INTERVAL))
    monkeypatch.setenv("MANTISFETCH_SEARCH_MAX_WAIT_SEC", "10")
    yield
    lb._next_search_allowed = {}


class _Hangs(SearchProvider):
    name = "p1"

    def __init__(self, hang: float):
        self.hang = hang

    async def search(self, query, *, max_results=10, lang="en", freshness=None):
        await asyncio.sleep(self.hang)
        raise SearchProviderUnavailable("p1 is down")


class _Records(SearchProvider):
    name = "p2"

    def __init__(self):
        self.calls: list[float] = []

    async def search(self, query, *, max_results=10, lang="en", freshness=None):
        self.calls.append(time.monotonic())
        return [SearchResult(url="https://p2.example", title="t", snippet="s",
                             published_at=None, score=1.0, provider="p2")]


def _factory(p1: SearchProvider, p2: _Records):
    def create(name=None):
        return p2 if name == "p2" else _FallbackSearchProvider([p1, p2])

    return create


async def test_a_concurrent_explicit_request_cannot_squeeze_in_before_the_fallback() -> None:
    p2 = _Records()
    transport = httpx.ASGITransport(app=lb.app)
    with patch("mantisfetch_browser.create_search_provider", side_effect=_factory(_Hangs(0.3), p2)):
        async with httpx.AsyncClient(transport=transport, base_url="http://web.test") as c:

            async def explicit_after(delay: float):
                await asyncio.sleep(delay)  # after the chain's up-front reservation lapsed
                return await c.post("/search", json={"query": "q", "provider": "p2"})

            chain, explicit = await asyncio.wait_for(
                asyncio.gather(c.post("/search", json={"query": "q"}), explicit_after(0.25)), 10
            )

    assert chain.status_code == 200 and explicit.status_code == 200
    assert len(p2.calls) == 2
    gap = abs(p2.calls[1] - p2.calls[0])
    assert gap >= INTERVAL - 0.01, f"p2 was hit twice {gap:.3f}s apart (interval {INTERVAL}s)"


async def test_a_fast_failover_does_not_wait_on_its_own_reservation() -> None:
    p2 = _Records()
    transport = httpx.ASGITransport(app=lb.app)
    with patch("mantisfetch_browser.create_search_provider", side_effect=_factory(_Hangs(0.0), p2)):
        async with httpx.AsyncClient(transport=transport, base_url="http://web.test") as c:
            started = time.monotonic()
            response = await c.post("/search", json={"query": "q"})
    assert response.status_code == 200
    assert p2.calls[0] - started < INTERVAL / 2, "the chain waited on its own reservation"


async def test_a_fallback_queue_past_the_budget_is_refused_not_slept_through() -> None:
    """The gate carries the #276 deadline, like the up-front throttle does."""
    p2 = _Records()
    lb._next_search_allowed["p2"] = time.monotonic() + 5  # p2's queue is long
    transport = httpx.ASGITransport(app=lb.app)
    with patch("mantisfetch_browser.create_search_provider", side_effect=_factory(_Hangs(0.1), p2)):
        async with httpx.AsyncClient(transport=transport, base_url="http://web.test") as c:
            started = time.monotonic()
            response = await asyncio.wait_for(
                c.post("/search_and_capture", json={"query": "q", "budget_seconds": 1.0}), 10
            )
            took = time.monotonic() - started
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "search_budget_exceeded"
    assert p2.calls == [] and took < 0.6


def test_with_a_gate_only_the_first_member_is_charged_up_front() -> None:
    chain = _FallbackSearchProvider([_Hangs(0), _Records()])
    assert chain.throttle_keys == ("p1", "p2")

    async def gate(name: str) -> None:
        return None

    chain.member_gate = gate
    assert chain.throttle_keys == ("p1",)

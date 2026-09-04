"""A burst of searches queues for its turn instead of being refused.

Issue #248, measured on a production instance: an agent issued eight parallel
`web_search` calls in one turn, twice. Both times one succeeded and six came
back `429: search rate limited: minimum interval not elapsed`. A 429 is not a
cheap "wait 2s and retry" for an agent — it costs another LLM round trip
(5-7s measured) before the call can be reissued, and after the first burst the
agent stopped searching in parallel at all.

The throttle exists to protect a paid search API's quota, and queueing respects
that just as well as refusing: eight searches at a 2s interval take 14s of wall
clock either way. What changes is that they all happen.
"""

import asyncio

import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    import mantisfetch_browser as lb

    lb._next_search_allowed = {}
    monkeypatch.setenv("MANTISFETCH_SEARCH_MIN_INTERVAL_SEC", "2")
    monkeypatch.setenv("MANTISFETCH_SEARCH_MAX_WAIT_SEC", "30")
    yield
    lb._next_search_allowed = {}


_REAL_SLEEP = asyncio.sleep


class _Clock:
    """A frozen clock that records what was slept on instead of sleeping.

    Frozen, not advancing: the reported burst arrived within a millisecond, so
    every caller should read the same `now` and take a ticket relative to it.
    A clock that jumped forward on each sleep would model the requests arriving
    one after another — which is the case that was never broken.

    Real sleeps would make this suite take a minute and still not pin the
    spacing; what is under test is the arithmetic of the queue.

    The yield uses the real asyncio.sleep captured at import: the fixture
    replaces the module attribute, so calling it by name here would recurse
    into this method.
    """

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        await _REAL_SLEEP(0)


@pytest.fixture()
def clock(monkeypatch):
    import mantisfetch_browser as lb

    c = _Clock()
    monkeypatch.setattr(lb.time, "monotonic", c.monotonic)
    monkeypatch.setattr(lb.asyncio, "sleep", c.sleep)
    return c


def _throttle(keys=("fake",)):
    import mantisfetch_browser as lb

    return lb._enforce_search_throttle(keys)


# ── the reported case ────────────────────────────────────────────────────────────
def test_eight_parallel_searches_all_get_through(clock) -> None:
    async def run():
        await asyncio.gather(*(_throttle() for _ in range(8)))

    asyncio.run(run())

    # Seven waited; the first went straight through.
    assert len(clock.slept) == 7
    assert clock.slept == pytest.approx([2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0])


def test_the_tickets_are_spaced_by_the_interval(clock) -> None:
    """Status codes alone would also pass if the calls simply raced through.
    The spacing is what proves the quota is still being respected."""
    import mantisfetch_browser as lb

    start = clock.now

    async def run():
        await asyncio.gather(*(_throttle() for _ in range(5)))

    asyncio.run(run())

    assert lb._next_search_allowed["fake"] == pytest.approx(start + 5 * 2.0)


def test_one_search_alone_does_not_wait(clock) -> None:
    asyncio.run(_throttle())
    assert clock.slept == []


# ── the bound, and what a refusal must not do ────────────────────────────────────
def test_a_queue_longer_than_the_bound_is_refused(clock, monkeypatch) -> None:
    monkeypatch.setenv("MANTISFETCH_SEARCH_MAX_WAIT_SEC", "5")

    async def run():
        return await asyncio.gather(
            *(_throttle() for _ in range(6)), return_exceptions=True
        )

    outcomes = asyncio.run(run())
    refused = [o for o in outcomes if isinstance(o, HTTPException)]

    # 0s, 2s, 4s waits fit under 5s; 6s, 8s, 10s do not.
    assert len(refused) == 3
    for exc in refused:
        assert exc.status_code == 429
        assert "Retry-After" in exc.headers
        assert int(exc.headers["Retry-After"]) >= 1
        assert "retry after" in str(exc.detail).lower()


def test_a_refused_search_takes_no_slot(clock, monkeypatch) -> None:
    """The property a naive implementation gets wrong: a caller told to come
    back must not also hold a turn it will never use, or the queue drifts
    further out on every refusal."""
    import mantisfetch_browser as lb

    monkeypatch.setenv("MANTISFETCH_SEARCH_MAX_WAIT_SEC", "3")

    async def run():
        await asyncio.gather(*(_throttle() for _ in range(2)))  # 0s and 2s
        before = lb._next_search_allowed["fake"]
        with pytest.raises(HTTPException):
            await _throttle()  # would be 4s, over the 3s bound
        return before, lb._next_search_allowed["fake"]

    before, after = asyncio.run(run())
    assert after == before


def test_zero_max_wait_restores_immediate_refusal(clock, monkeypatch) -> None:
    """The original behaviour stays available as a setting — minus the
    bareness: even the immediate 429 now says when to come back."""
    monkeypatch.setenv("MANTISFETCH_SEARCH_MAX_WAIT_SEC", "0")

    async def run():
        await _throttle()
        with pytest.raises(HTTPException) as caught:
            await _throttle()
        return caught.value

    exc = asyncio.run(run())
    assert exc.status_code == 429
    assert exc.headers["Retry-After"] == "2"


def test_no_interval_means_no_throttle_at_all(clock, monkeypatch) -> None:
    monkeypatch.setenv("MANTISFETCH_SEARCH_MIN_INTERVAL_SEC", "0")

    async def run():
        await asyncio.gather(*(_throttle() for _ in range(5)))

    asyncio.run(run())
    assert clock.slept == []


# ── one provider's queue must not stall another ──────────────────────────────────
def test_a_queue_on_one_provider_does_not_delay_another(clock) -> None:
    """The reason the sleep is outside the lock. Holding it while waiting would
    serialise bocha behind tavily for no quota reason."""

    async def run():
        await asyncio.gather(*(_throttle(("bocha",)) for _ in range(4)))
        clock.slept.clear()
        await _throttle(("tavily",))

    asyncio.run(run())
    assert clock.slept == []


def test_a_fallback_chain_charges_every_member(clock, monkeypatch) -> None:
    """Failover must not be a way around the limit: a search that may touch
    both backends books a slot on both."""
    monkeypatch.setenv("MANTISFETCH_SEARCH_MAX_WAIT_SEC", "0")

    async def run():
        await _throttle(("bocha", "tavily"))
        with pytest.raises(HTTPException):
            await _throttle(("tavily",))

    asyncio.run(run())


# ── cancellation ─────────────────────────────────────────────────────────────────
def test_a_cancelled_caller_does_not_hold_the_lock() -> None:
    """The sleep is outside the lock, so a client that disconnects mid-wait
    cannot wedge every later search. Runs on the real clock — the point is what
    asyncio does with the cancellation, not the arithmetic."""
    import mantisfetch_browser as lb

    lb._next_search_allowed = {}

    async def run():
        await _throttle()  # takes the first slot, no wait
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_throttle(), timeout=0.05)  # cancelled mid-sleep
        assert not lb._search_throttle_lock.locked()
        # and the queue still moves
        await asyncio.wait_for(_throttle(("other",)), timeout=1.0)

    asyncio.run(run())

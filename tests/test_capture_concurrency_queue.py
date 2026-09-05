"""A burst of captures waits for a slot instead of being refused on sight.

Same fault as the search throttle in #248, one gate over. Measured on the
delivery image against real pages:

    burst  succeeded  median   wall     throughput
        8        8/8   1.53s   2.89s     2.8/s
       16      16/16   1.84s   3.81s     4.2/s
       24      24/24   2.01s   5.87s     4.1/s
       32      32/32   2.77s   6.99s     4.6/s
       80      80/80  17.94s  19.04s     4.2/s

Throughput saturates at ~4.5 captures/second from 16 concurrent onward, so 16
is the gate; past it, concurrency buys latency rather than work. Under the old
gate of 10 a burst of 16 produced exactly 10 successes and 6 instant 429s —
verified on the same image — and each of those cost the agent an LLM round trip
to discover.

The ceiling is that machine's. A customer box saturates somewhere else, which
is why the queue matters more than the number.
"""

import asyncio

import pytest
from fastapi import HTTPException

_REAL_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def _bound(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_CONCURRENCY_MAX_WAIT_SEC", "30")
    yield


@pytest.fixture()
def gate(monkeypatch):
    """A capture gate of 4, with slots held until the test lets go.

    Four rather than sixteen so the tests say what they mean without sixteen
    parked coroutines; the arithmetic is the same.
    """
    import mantisfetch_browser as lb

    sem = asyncio.Semaphore(4)
    monkeypatch.setattr(lb, "_capture_sem", sem)
    return sem


async def _hold(released: asyncio.Event, done: list) -> None:
    """Take a slot the way _capture_fresh does, and keep it until released."""
    import mantisfetch_browser as lb

    await lb._acquire_or_refuse(lb._capture_sem, "too many concurrent captures")
    try:
        await released.wait()
        done.append(True)
    finally:
        lb._capture_sem.release()


async def _spin_until(predicate, what: str, turns: int = 2000) -> None:
    for _ in range(turns):
        if predicate():
            return
        await _REAL_SLEEP(0)
    raise AssertionError(f"never reached: {what}")


# ── the reported shape: a burst waits, it does not fail ──────────────────────────
def test_a_burst_over_the_gate_waits_and_all_of_it_completes(gate) -> None:
    async def run():
        released = asyncio.Event()
        done: list = []
        callers = [asyncio.create_task(_hold(released, done)) for _ in range(10)]

        # Four hold slots; six are queued on the semaphore, not refused.
        await _spin_until(lambda: gate.locked(), "the gate to fill")
        assert len(done) == 0
        assert not any(t.done() for t in callers), "someone was refused instead of queued"

        released.set()
        await asyncio.wait_for(asyncio.gather(*callers), timeout=5)
        assert len(done) == 10

    asyncio.run(run())


def test_within_the_gate_nothing_waits(gate) -> None:
    async def run():
        released = asyncio.Event()
        released.set()
        await asyncio.wait_for(
            asyncio.gather(*(_hold(released, []) for _ in range(4))), timeout=5
        )

    asyncio.run(run())


# ── the bound ────────────────────────────────────────────────────────────────────
def test_a_wait_past_the_bound_is_refused_with_retry_after(gate, monkeypatch) -> None:
    monkeypatch.setenv("MANTISFETCH_CONCURRENCY_MAX_WAIT_SEC", "0.05")

    async def run():
        import mantisfetch_browser as lb

        held = asyncio.Event()
        holders = [asyncio.create_task(_hold(held, [])) for _ in range(4)]
        await _spin_until(lambda: gate.locked(), "the gate to fill")

        with pytest.raises(HTTPException) as caught:
            await lb._acquire_or_refuse(gate, "too many concurrent captures")

        held.set()
        await asyncio.gather(*holders)
        return caught.value

    exc = asyncio.run(run())
    assert exc.status_code == 429
    assert "too many concurrent captures" in str(exc.detail)
    assert "no slot freed within" in str(exc.detail)
    assert exc.headers["Retry-After"] == "1"


def test_zero_bound_refuses_immediately(gate, monkeypatch) -> None:
    """The original fail-fast stays available as a setting — with the header."""
    monkeypatch.setenv("MANTISFETCH_CONCURRENCY_MAX_WAIT_SEC", "0")

    async def run():
        import mantisfetch_browser as lb

        held = asyncio.Event()
        holders = [asyncio.create_task(_hold(held, [])) for _ in range(4)]
        await _spin_until(lambda: gate.locked(), "the gate to fill")

        with pytest.raises(HTTPException) as caught:
            await lb._acquire_or_refuse(gate, "too many concurrent captures")

        held.set()
        await asyncio.gather(*holders)
        return caught.value

    exc = asyncio.run(run())
    assert exc.status_code == 429
    assert "Retry-After" in exc.headers


def test_a_refusal_does_not_consume_a_slot(gate, monkeypatch) -> None:
    """A refused caller must not have taken the slot it was told it cannot have,
    or the gate leaks capacity on every refusal."""
    monkeypatch.setenv("MANTISFETCH_CONCURRENCY_MAX_WAIT_SEC", "0")

    async def run():
        import mantisfetch_browser as lb

        held = asyncio.Event()
        holders = [asyncio.create_task(_hold(held, [])) for _ in range(4)]
        await _spin_until(lambda: gate.locked(), "the gate to fill")

        for _ in range(3):
            with pytest.raises(HTTPException):
                await lb._acquire_or_refuse(gate, "too many concurrent captures")

        held.set()
        await asyncio.gather(*holders)
        # All four slots are back, so four more callers fit.
        again = asyncio.Event()
        again.set()
        await asyncio.wait_for(
            asyncio.gather(*(_hold(again, []) for _ in range(4))), timeout=5
        )

    asyncio.run(run())


# ── cancellation ─────────────────────────────────────────────────────────────────
def test_a_caller_cancelled_while_queued_does_not_leak_its_slot(gate) -> None:
    """asyncio.Semaphore.acquire restores the counter and wakes the next waiter
    on CancelledError (3.11.15, the version the image ships). Pinned here
    because the whole queue depends on it."""
    import mantisfetch_browser as lb

    async def run():
        held = asyncio.Event()
        holders = [asyncio.create_task(_hold(held, [])) for _ in range(4)]
        await _spin_until(lambda: gate.locked(), "the gate to fill")

        doomed = asyncio.create_task(
            lb._acquire_or_refuse(gate, "too many concurrent captures")
        )
        await _REAL_SLEEP(0)
        doomed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await doomed

        held.set()
        await asyncio.gather(*holders)

        # Nothing was lost: the gate is whole again.
        again = asyncio.Event()
        again.set()
        await asyncio.wait_for(
            asyncio.gather(*(_hold(again, []) for _ in range(4))), timeout=5
        )

    asyncio.run(run())


# ── the sessions gate, same treatment ────────────────────────────────────────────
def test_the_session_gate_queues_too(monkeypatch) -> None:
    """Creating a session takes 0.03s idle and 0.29s with 200 already live, so a
    burst of twenty drains in about a third of a second. Refusing bought no
    resource protection — measured, not assumed."""
    import mantisfetch_browser as lb

    sem = asyncio.Semaphore(2)
    monkeypatch.setattr(lb, "_session_sem", sem)

    async def run():
        released = asyncio.Event()
        done: list = []

        async def one():
            await lb._acquire_or_refuse(sem, "too many concurrent session creations")
            try:
                await released.wait()
                done.append(True)
            finally:
                sem.release()

        callers = [asyncio.create_task(one()) for _ in range(6)]
        await _spin_until(lambda: sem.locked(), "the gate to fill")
        assert not any(t.done() for t in callers)

        released.set()
        await asyncio.wait_for(asyncio.gather(*callers), timeout=5)
        assert len(done) == 6

    asyncio.run(run())

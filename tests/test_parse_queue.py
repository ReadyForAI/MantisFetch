"""A parse waits for its slot inside the budget the caller declared.

Measured on the delivery image, and the numbers are why waiting is right rather
than a bigger gate:

  single parse   xlsx 0.03s | text PDF 40p 2.33s | scanned 8p (OCR) 16.5s
  text sweep     1->16 concurrent: throughput 0.43 -> 0.32/s, CPU 105% of 1600%
  OCR sweep      1->4  concurrent: throughput 0.06 -> 0.05/s, CPU 1066%

Concurrency buys nothing at any level, for opposite reasons: text parsing is
GIL-bound, and PaddleOCR already spreads one document across ~10 cores. So a
refused caller gains nothing by retrying sooner than a queued one would have
finished — it only pays an LLM round trip to learn that.

The gate refused rather than queued on the stated grounds that queueing would
pile scratch files. That was measured false: with the gate at 2, a burst of six
wrote all six scratch files before four were refused, because the upload is
staged before the gate is consulted. Refusing discarded an upload already paid
for. The disk cost is real but belongs to the queue's length, which is why it
is bounded by bytes here rather than by refusing.
"""

import asyncio

import pytest
from starlette.testclient import TestClient

PDF = b"%PDF-1.4 minimal but openable enough to reach the gate"
HTML = b"<h1>T</h1><p>Body text worth keeping.</p>"


@pytest.fixture()
def docs_dir(monkeypatch, tmp_path):
    import mantisfetch_common.storage as cs

    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


@pytest.fixture()
def full_gate(monkeypatch):
    """A gate with no free slot, and a way to give one back.

    The release path is half of what is under test — a gate that can never open
    would only prove refusals — so one test hands a slot back mid-wait and
    expects the parse to proceed.
    """
    import mantisfetch_docreader as dr

    sem = asyncio.Semaphore(0)
    monkeypatch.setattr(dr, "_parse_sem", sem)
    return sem


def _post(client: TestClient, docs_dir, content=HTML, name="doc.html", **extra):
    return client.post(
        "/doc/parse",
        files={"file": (name, content, "application/octet-stream")},
        data={"summary_mode": "off", "generate_summary": "false", **extra},
    )


# ── the budget is what bounds the wait ───────────────────────────────────────────
def test_a_declared_budget_bounds_the_queue_wait(client, docs_dir, full_gate) -> None:
    """No slot will free, so the answer must arrive by the budget rather than
    when the gate happens to open."""
    resp = _post(client, docs_dir, budget_seconds="0.4")

    assert resp.status_code == 422
    body = resp.json()["detail"]
    assert body["error"] == "parse_budget_exceeded"
    assert body["budget_seconds"] == 0.4
    assert "queued_seconds" in body
    assert "waited" in body["message"] and "slot" in body["message"]


def test_the_refusal_says_which_half_of_the_budget_ran_out(
    client, docs_dir, full_gate
) -> None:
    """Two refusals share the error code because the remedy is the same. A
    caller tuning its budget still needs to know whether the document was too
    slow or the queue was too long."""
    queued = _post(client, docs_dir, budget_seconds="0.4").json()["detail"]
    assert "waited" in queued["message"]
    assert queued["queued_seconds"] >= 0


def test_a_slot_that_frees_inside_the_budget_is_used(monkeypatch) -> None:
    """The point of the change: a caller behind a short parse gets the slot
    rather than being told to go away.

    Driven at the coroutine level. Releasing from a threading.Timer looked
    simpler and was wrong: asyncio.Semaphore.release from another thread does
    not wake the loop's waiter, so the test sat until the budget expired and
    passed for the wrong reason — 30s per run, which is how it was noticed.
    """
    import mantisfetch_docreader as dr

    sem = asyncio.Semaphore(0)
    monkeypatch.setattr(dr, "_parse_sem", sem)

    async def run():
        import time as _time

        async def free_it():
            await asyncio.sleep(0.05)
            sem.release()

        asyncio.create_task(free_it())
        t0 = _time.monotonic()
        async with dr._parse_slot(budget_seconds=30, t_entry=t0, estimate=None):
            waited = _time.monotonic() - t0
        return waited

    waited = asyncio.run(run())
    assert 0.04 < waited < 5, f"took {waited:.2f}s — did it wait for the release?"


def test_the_slot_is_given_back_after_the_parse(monkeypatch) -> None:
    """Two callers in a row on a gate of one: the second only gets in if the
    first released."""
    import mantisfetch_docreader as dr

    sem = asyncio.Semaphore(1)
    monkeypatch.setattr(dr, "_parse_sem", sem)

    async def run():
        import time as _time

        for _ in range(3):
            async with dr._parse_slot(
                budget_seconds=5, t_entry=_time.monotonic(), estimate=None
            ):
                pass
        return sem.locked()

    assert asyncio.run(run()) is False


def test_without_a_budget_the_queue_ceiling_applies(client, docs_dir, full_gate, monkeypatch) -> None:
    """"No budget" means "I can wait", not "wait forever": two wedged OCR jobs
    would otherwise hold every later caller indefinitely."""
    monkeypatch.setenv("MANTISFETCH_PARSE_QUEUE_MAX_WAIT_SEC", "0.3")

    resp = _post(client, docs_dir)

    assert resp.status_code == 429
    assert "no slot freed within" in resp.json()["detail"]
    assert resp.headers["retry-after"]


def test_an_open_gate_does_not_wait(client, docs_dir) -> None:
    resp = _post(client, docs_dir, budget_seconds="30")
    assert resp.status_code == 200


# ── what a refusal must not leave behind ─────────────────────────────────────────
def test_a_queue_refusal_leaves_no_failure_record(client, docs_dir, full_gate) -> None:
    """A parse that never started is not a parse that failed. Leaving a
    .parse-failed.json would collapse the four on-disk states #209 keeps
    distinct."""
    import mantisfetch_docreader as dr

    _post(client, docs_dir, budget_seconds="0.3")

    general = docs_dir / "General"
    markers = list(general.glob("DOC-*/" + dr.PARSE_FAILURE_MARKER)) if general.exists() else []
    assert markers == []


def test_a_queue_refusal_does_not_strand_the_doc_id(client, docs_dir, full_gate) -> None:
    """The in-flight reservation lives in a WeakValueDictionary keyed by
    doc_id. If a refusal left its entry behind, source_filename ingests of the
    same name would roll to -2 forever."""
    import gc

    import mantisfetch_docreader as dr

    _post(client, docs_dir, budget_seconds="0.3", id_strategy="source_filename", name="report.html")
    gc.collect()

    assert "report" not in dr._doc_id_parse_locks


# ── the bytes bound on the queue ─────────────────────────────────────────────────
def test_the_queue_is_bounded_by_staged_bytes(client, docs_dir, full_gate, monkeypatch) -> None:
    """The disk a queue holds is its real cost, so that is what bounds it."""
    monkeypatch.setenv("MANTISFETCH_PARSE_QUEUE_MAX_BYTES", "10")

    resp = _post(client, docs_dir, content=b"x" * 200 + HTML, budget_seconds="0.3")

    assert resp.status_code == 429
    assert "staged uploads" in resp.json()["detail"]
    assert resp.headers["retry-after"]


def test_the_bytes_counter_is_given_back(client, docs_dir) -> None:
    """Held bytes are released whether the parse succeeded, failed or was
    refused — otherwise the cap ratchets shut over a day of traffic."""
    import mantisfetch_docreader as dr

    before = dr._scratch_bytes_held
    assert _post(client, docs_dir).status_code == 200
    assert _post(client, docs_dir, content=b"", name="empty.txt").status_code == 422
    assert dr._scratch_bytes_held == before


def test_an_exhausted_budget_still_takes_a_free_slot(client, docs_dir) -> None:
    """The budget bounds the queue; it does not deny an open gate.

    A caller whose cost cannot be estimated is never refused on a guess
    (test_unknown_cost_is_never_refused). Subtracting elapsed time from a tiny
    budget and refusing would have broken that for every HTML upload, which is
    how this rule was found.
    """
    resp = _post(client, docs_dir, budget_seconds="0.001")
    assert resp.status_code == 200


def test_through_the_endpoint_a_freed_slot_completes_the_parse(
    client, docs_dir, monkeypatch
) -> None:
    """The handler-level proof that it queues.

    The coroutine-level test above exercises `_parse_slot`, which a fail-fast
    pre-check in the handler would sail straight past — so it cannot show that
    the endpoint waits. This one can: the gate is full when the request
    arrives, frees while it is queued, and the parse has to complete.

    The slot is freed with `call_later` from inside the loop. A threading.Timer
    does not wake an asyncio waiter, which is a mistake already made once here.
    """
    import mantisfetch_docreader as dr

    class _FreesWhileYouWait(asyncio.Semaphore):
        armed = True

        async def acquire(self):
            if self.armed and self.locked():
                self.armed = False
                asyncio.get_running_loop().call_later(0.05, self.release)
            return await super().acquire()

    monkeypatch.setattr(dr, "_parse_sem", _FreesWhileYouWait(0))

    resp = _post(client, docs_dir, budget_seconds="30")
    assert resp.status_code == 200, resp.json()
    assert resp.json()["doc_id"]

"""D1: gemini_summarize must let concurrent workers overlap (the lock guards only
the rate-limit slot reservation, not the network call), while still spacing
request starts by the min-interval."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import mantisfetch_docreader.summaries as s


def test_gemini_summarize_runs_concurrently(monkeypatch):
    monkeypatch.setattr(s, "SUMMARY_REQUEST_MIN_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(s, "_summary_llm_sem", threading.BoundedSemaphore(4))
    monkeypatch.setattr(s, "_summary_llm_next_allowed_at", 0.0)

    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    class _Provider:
        def summarize(self, text, prompt, max_retries=2):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.1)
            with lock:
                state["active"] -= 1
            return "ok"

    monkeypatch.setattr("providers.get_provider", lambda: _Provider())

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(s.gemini_summarize, "text", "prompt") for _ in range(4)]
        assert [f.result() for f in futures] == ["ok"] * 4

    # Under the old lock-around-network this would be 1 (fully serial).
    assert state["max_active"] >= 2, "summary LLM calls did not overlap"


def test_gemini_summarize_still_spaces_request_starts(monkeypatch):
    monkeypatch.setattr(s, "SUMMARY_REQUEST_MIN_INTERVAL_SEC", 0.2)
    monkeypatch.setattr(s, "_summary_llm_sem", threading.BoundedSemaphore(4))
    monkeypatch.setattr(s, "_summary_llm_next_allowed_at", 0.0)

    starts: list[float] = []
    starts_lock = threading.Lock()

    class _Provider:
        def summarize(self, text, prompt, max_retries=2):
            with starts_lock:
                starts.append(time.monotonic())
            return "ok"

    monkeypatch.setattr("providers.get_provider", lambda: _Provider())

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: s.gemini_summarize("text", "prompt"), range(3)))

    starts.sort()
    gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    assert all(g >= 0.15 for g in gaps), f"min-interval not enforced: {gaps}"

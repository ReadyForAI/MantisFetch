"""Process-wide cumulative counters for token-efficiency and failover observability.

Thread-safe increments; ``snapshot()`` returns a plain dict suitable for
``GET /metrics`` or embedding in ``/health``. Counters are in-process only
(reset on restart) — enough to back README claims and debug failover rates
without a metrics stack.
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_counts: dict[str, int] = {
    # distill
    "distill_input_chars": 0,
    "distill_output_chars": 0,
    "distill_calls": 0,
    # capture cache
    "capture_cache_hits": 0,
    "capture_cache_misses": 0,
    "capture_content_hash_hits": 0,
    # OCR
    "ocr_pages": 0,
    "ocr_cache_hits": 0,
    # summary LLM
    "summary_llm_calls": 0,
    # failover
    "failover_summary": 0,
    "failover_ocr": 0,
}


def incr(name: str, n: int = 1) -> None:
    """Increment counter ``name`` by ``n`` (no-op for unknown names)."""
    if n == 0:
        return
    with _lock:
        if name in _counts:
            _counts[name] += n


def snapshot() -> dict[str, Any]:
    """Return a copy of all counters plus derived ratios."""
    with _lock:
        data = dict(_counts)
    distill_in = data["distill_input_chars"]
    distill_out = data["distill_output_chars"]
    cache_hits = data["capture_cache_hits"]
    cache_misses = data["capture_cache_misses"]
    cache_total = cache_hits + cache_misses
    ocr_pages = data["ocr_pages"]
    ocr_hits = data["ocr_cache_hits"]
    data["ratios"] = {
        "distill_output_over_input": (
            round(distill_out / distill_in, 4) if distill_in else None
        ),
        "capture_cache_hit_rate": (
            round(cache_hits / cache_total, 4) if cache_total else None
        ),
        "ocr_cache_hit_rate": (round(ocr_hits / ocr_pages, 4) if ocr_pages else None),
    }
    return data


def reset() -> None:
    """Zero all counters (tests only)."""
    with _lock:
        for k in _counts:
            _counts[k] = 0

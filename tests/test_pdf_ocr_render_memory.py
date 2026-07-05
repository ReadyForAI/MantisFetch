"""B4: PDF OCR must not hold every page's rendered PNG in memory at once.

Pages are rendered to a scratch file and queued by path; each worker reads its
PNG then deletes it, so peak resident PNG memory is ~O(concurrency), not
O(page_count) — the difference between MBs and GBs on a few-hundred-page scan.
"""

import threading
import time

import pytest


def _make_scan_pdf(path, pages: int) -> None:
    import fitz

    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=320, height=440)
        page.draw_rect(
            fitz.Rect(15, 15, 305, 425), color=(0, 0, 0), fill=(0.92, 0.92, 0.92)
        )
        page.insert_text((30, 220), f"page {i + 1}")
    doc.save(str(path))
    doc.close()


def test_pdf_ocr_render_memory_bounded(tmp_path, monkeypatch):
    pytest.importorskip("fitz")
    import mantisfetch_docreader as dr

    pdf = tmp_path / "scan.pdf"
    _make_scan_pdf(pdf, pages=8)

    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "calls": 0, "bad_png": 0, "pages": set()}

    def _track(img_bytes: bytes, page_num: int) -> None:
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["calls"] += 1
            state["pages"].add(page_num)
            if not img_bytes.startswith(b"\x89PNG"):
                state["bad_png"] += 1
        time.sleep(0.03)  # hold the bytes so concurrent workers overlap
        with lock:
            state["active"] -= 1

    def fake_local(img_bytes, page_num, backend):
        _track(img_bytes, page_num)
        return f"ocr page {page_num}", None

    def fake_gemini(img_bytes, page_num, proofread=True):
        _track(img_bytes, page_num)
        return f"ocr page {page_num}"

    monkeypatch.setattr(dr, "local_ocr_with_layout", fake_local)
    monkeypatch.setattr(dr, "gemini_ocr", fake_gemini)

    parsed = dr.parse_pdf(pdf, force_ocr=True, concurrency=2)

    assert parsed.total_pages == 8
    assert len(state["pages"]) == 8, "not every page was OCR'd"
    assert state["bad_png"] == 0, "a worker received bytes that weren't a PNG"
    # The core guarantee: never more than `concurrency` PNGs resident at once
    # (would be up to 8 if the queue held bytes instead of paths).
    assert state["peak"] <= 2, f"peak resident PNGs {state['peak']} exceeded concurrency"

"""The OCR prewarm does not stall the event loop (F04).

`api_parse_doc` is async, and the prewarm used to run in it directly: it reads
the PDF to decide whether OCR will be needed, then takes the local OCR worker
lock to start the worker. That lock is held by whichever thread is OCRing a
page, for as long as the page takes — up to the request timeout. A second
scanned PDF arriving meanwhile froze every request on the process, /health
included, until the first page finished.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import httpx
import mantisfetch_docreader as dr
import pytest

import mantisfetch_common.storage as cs

LOCK_HELD_SEC = 0.4


@pytest.fixture()
def docs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


def _scanned_pdf(path: Path) -> bytes:
    import fitz

    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()
    return path.read_bytes()


def _parsed() -> dr.ParsedDocument:
    return dr.ParsedDocument(
        filename="scan.pdf",
        file_type="pdf",
        total_pages=1,
        pages=[],
        sections=[
            dr.Section(index=1, title="S", level=1, text="text", page_range="1-1", sid="s1", summary="")
        ],
        ocr_page_count=0,
        table_count=0,
    )


async def test_a_busy_ocr_worker_does_not_freeze_other_requests(
    docs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dr, "PREWARM_LOCAL_OCR", True)
    monkeypatch.setattr(dr, "_should_prewarm_local_ocr_for_pdf", lambda *a, **k: True)
    monkeypatch.setattr(dr, "_get_local_ocr_worker", lambda: None)
    monkeypatch.setattr(dr, "parse_pdf", lambda *a, **k: _parsed())

    # Another document's page is being OCRed: the worker lock is taken.
    taken = threading.Event()

    def ocr_a_page() -> None:
        with dr._local_ocr_worker_lock:
            taken.set()
            time.sleep(LOCK_HELD_SEC)

    busy = threading.Thread(target=ocr_a_page)
    busy.start()
    assert taken.wait(2)

    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    transport = httpx.ASGITransport(app=dr.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://doc.test") as client:
        response = await client.post(
            "/parse",
            files={"file": ("scan.pdf", _scanned_pdf(tmp_path / "scan.pdf"), "application/pdf")},
            data={"doc_id": "DOC-8001", "summary_mode": "off"},
        )
    stop.set()
    await tick
    busy.join()

    assert response.status_code == 200, response.text
    worst = max(gaps)
    assert worst < LOCK_HELD_SEC / 2, (
        f"the event loop stalled {worst * 1000:.0f} ms while another page held the OCR lock"
    )


async def test_the_budget_estimate_does_not_freeze_other_requests(
    docs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The estimate opens the PDF and inspects every page — a long one takes time."""
    def slow_estimate(*a, **k):
        time.sleep(LOCK_HELD_SEC)
        return None

    monkeypatch.setattr(dr, "_estimate_parse_seconds", slow_estimate)
    monkeypatch.setattr(dr, "PREWARM_LOCAL_OCR", False)
    monkeypatch.setattr(dr, "parse_pdf", lambda *a, **k: _parsed())

    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    transport = httpx.ASGITransport(app=dr.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://doc.test") as client:
        response = await client.post(
            "/parse",
            files={"file": ("scan.pdf", _scanned_pdf(tmp_path / "scan.pdf"), "application/pdf")},
            data={"doc_id": "DOC-8002", "summary_mode": "off", "budget_seconds": "60"},
        )
    stop.set()
    await tick

    assert response.status_code == 200, response.text
    worst = max(gaps)
    assert worst < LOCK_HELD_SEC / 2, f"the event loop stalled {worst * 1000:.0f} ms on the estimate"

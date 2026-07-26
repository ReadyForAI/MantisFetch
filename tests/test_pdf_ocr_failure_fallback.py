"""OCR failure handling: local fallback for failed LLM upgrades + honest accounting.

Observed on a real 162-page scan: the provider timed out on 4 of 29 upgraded
pages and those pages lost text/tables the local worker had already produced in
the prior pass (`ocr_pages` renders upgrade pages only for the LLM); separately,
an all-pages OCR failure still reported `ocr_page_count == total_pages` with no
failure signal anywhere in the response or manifest.
"""

import threading

import pytest


def _make_scan_pdf(path, pages: int) -> None:
    import fitz

    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=320, height=440)
        page.draw_rect(fitz.Rect(15, 15, 305, 425), color=(0, 0, 0), fill=(0.92, 0.92, 0.92))
        page.insert_text((30, 220), f"page {i + 1}")
    doc.save(str(path))
    doc.close()


@pytest.fixture
def scan_pdf(tmp_path):
    pytest.importorskip("fitz")
    pdf = tmp_path / "scan.pdf"
    _make_scan_pdf(pdf, pages=4)
    return pdf


def test_failed_llm_upgrade_falls_back_to_local(scan_pdf, monkeypatch):
    """An `ocr_pages` upgrade page whose LLM OCR fails must keep the local result
    instead of ending up with no content at all."""
    import mantisfetch_docreader as dr

    lock = threading.Lock()
    local_pages = set()

    def fake_local(img_bytes, page_num, backend):
        with lock:
            local_pages.add(page_num)
        return f"local text page {page_num}", None

    def fake_gemini(img_bytes, page_num, proofread=True):
        return f"[OCR failed: provider timeout p{page_num}]"

    monkeypatch.setattr(dr, "local_ocr_with_layout", fake_local)
    monkeypatch.setattr(dr, "gemini_ocr", fake_gemini)

    parsed = dr.parse_pdf(scan_pdf, ocr_pages_spec="2", concurrency=2)

    # The failed upgrade page fell back to a local pass...
    assert 2 in local_pages
    joined = "\n".join(p.text for p in parsed.pages)
    assert "local text page 2" in joined
    assert "[OCR failed" not in joined
    # ...so nothing failed from the caller's point of view.
    assert parsed.ocr_failed_pages == []
    assert parsed.ocr_page_count == 4


def test_successful_llm_upgrade_does_not_run_local_fallback(scan_pdf, monkeypatch):
    import mantisfetch_docreader as dr

    lock = threading.Lock()
    local_pages = set()

    def fake_local(img_bytes, page_num, backend):
        with lock:
            local_pages.add(page_num)
        return f"local text page {page_num}", None

    def fake_gemini(img_bytes, page_num, proofread=True):
        return f"llm text page {page_num}"

    monkeypatch.setattr(dr, "local_ocr_with_layout", fake_local)
    monkeypatch.setattr(dr, "gemini_ocr", fake_gemini)

    parsed = dr.parse_pdf(scan_pdf, ocr_pages_spec="2", concurrency=2)

    assert 2 not in local_pages  # upgrade succeeded — no wasted local pass
    assert "llm text page 2" in "\n".join(p.text for p in parsed.pages)
    assert parsed.ocr_failed_pages == []
    assert parsed.ocr_page_count == 4


def test_all_pages_failed_ocr_is_reported_not_hidden(scan_pdf, monkeypatch):
    """force_ocr routes every page to the LLM with no local fallback in the plan;
    when all of it fails the parse must say so instead of reporting full OCR."""
    import mantisfetch_docreader as dr

    def fake_gemini(img_bytes, page_num, proofread=True):
        return "[OCR failed: provider down]"

    def fake_local(img_bytes, page_num, backend):
        # The scan-like fallback kicks in after the LLM failures; keep it failing
        # too so "all backends failed" is what the test actually exercises
        # regardless of whether a real local worker exists on this machine.
        return "[OCR failed: no local worker]", None

    monkeypatch.setattr(dr, "gemini_ocr", fake_gemini)
    monkeypatch.setattr(dr, "local_ocr_with_layout", fake_local)

    parsed = dr.parse_pdf(scan_pdf, force_ocr=True, concurrency=2)

    assert parsed.ocr_page_count == 0
    assert parsed.ocr_failed_pages == [1, 2, 3, 4]
    assert parsed.metadata["ocr_failed_pages"] == [1, 2, 3, 4]


def test_partial_local_failure_counts_only_successes(scan_pdf, monkeypatch):
    import mantisfetch_docreader as dr

    def fake_local(img_bytes, page_num, backend):
        if page_num == 3:
            return "[OCR failed: worker crash]", None
        return f"local text page {page_num}", None

    monkeypatch.setattr(dr, "local_ocr_with_layout", fake_local)

    parsed = dr.parse_pdf(scan_pdf, concurrency=2)

    assert parsed.ocr_page_count == 3
    assert parsed.ocr_failed_pages == [3]


def test_manifest_carries_ocr_failure_fields(scan_pdf, tmp_path, monkeypatch):
    import json

    import mantisfetch_docreader as dr

    def fake_gemini(img_bytes, page_num, proofread=True):
        return "[OCR failed: provider down]"

    def fake_local(img_bytes, page_num, backend):
        return "[OCR failed: no local worker]", None

    monkeypatch.setattr(dr, "gemini_ocr", fake_gemini)
    monkeypatch.setattr(dr, "local_ocr_with_layout", fake_local)

    parsed = dr.parse_pdf(scan_pdf, force_ocr=True, concurrency=2)
    docs_dir = tmp_path / "docs"
    dr.write_output_extract_only("DOC-T01", parsed, docs_dir, content_type="General")

    manifest = json.loads((docs_dir / "General" / "DOC-T01" / "manifest.json").read_text())
    assert manifest["ocr_page_count"] == 0
    assert manifest["ocr_failed_page_count"] == 4
    assert manifest["ocr_failed_pages"] == [1, 2, 3, 4]

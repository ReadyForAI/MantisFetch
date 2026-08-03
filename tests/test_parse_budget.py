"""A call that cannot afford a document is refused up front, not timed out.

SharedSpecs IRP 20260801 §2.1: the criterion lives in MantisFetch (a cheap
pre-flight estimate), the budget travels with the request (`budget_seconds`,
single source of truth at the caller), and refusal is per-caller — the same
document is refused on the MCP leg and accepted on a background REST leg.
"""

from __future__ import annotations

from pathlib import Path

import mantisfetch_docreader as dr
import pytest
from starlette.testclient import TestClient

import mantisfetch_common.storage as cs


def _scanned_pdf(path: Path, pages: int) -> bytes:
    """A PDF whose pages carry no text layer — i.e. what a scan looks like."""
    import fitz

    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()
    return path.read_bytes()


def _native_pdf(path: Path, pages: int) -> bytes:
    import fitz

    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), "Payment terms require invoice submission within 30 days. " * 3)
    doc.save(str(path))
    doc.close()
    return path.read_bytes()


@pytest.fixture()
def docs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


@pytest.fixture()
def doc_client() -> TestClient:
    return TestClient(dr.app, raise_server_exceptions=False)


# ── the estimate itself ────────────────────────────────────────────────────────


def test_estimate_separates_scans_from_native_text(tmp_path: Path) -> None:
    scan = tmp_path / "scan.pdf"
    _scanned_pdf(scan, 120)
    native = tmp_path / "native.pdf"
    _native_pdf(native, 30)

    scanned = dr._estimate_parse_seconds(scan, ".pdf")
    assert scanned["pages"] == 120
    assert scanned["ocr_pages"] == 120
    assert scanned["estimated_seconds"] > 100

    plain = dr._estimate_parse_seconds(native, ".pdf")
    assert plain["pages"] == 30
    assert plain["ocr_pages"] == 0
    assert plain["estimated_seconds"] == 0


def test_estimate_is_none_when_it_cannot_be_made(tmp_path: Path) -> None:
    """None means unknown. A caller must not read it as cheap."""
    f = tmp_path / "page.html"
    f.write_bytes(b"<p>hi</p>")
    assert dr._estimate_parse_seconds(f, ".html") is None

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really")
    assert dr._estimate_parse_seconds(broken, ".pdf") is None


# ── the refusal ────────────────────────────────────────────────────────────────


def _post(client: TestClient, content: bytes, doc_id: str, **extra):
    return client.post(
        "/parse",
        files={"file": ("doc.pdf", content, "application/pdf")},
        data={"doc_id": doc_id, "summary_mode": "off", **extra},
    )


def test_over_budget_is_refused_before_anything_is_spent(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path
) -> None:
    content = _scanned_pdf(tmp_path / "s.pdf", 120)
    resp = _post(doc_client, content, "DOC-5001", budget_seconds="30")

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "parse_budget_exceeded"
    assert detail["pages"] == 120
    assert detail["estimated_seconds"] > 30
    assert detail["budget_seconds"] == 30
    # nothing started: no directory, and the id was not consumed
    assert not (docs_dir / "General" / "DOC-5001").exists()
    assert not (docs_dir / ".counter").exists()
    # and nothing left staged — a refusal must not strand the upload, which can
    # be up to MAX_UPLOAD_BYTES and would accumulate on every refusal
    scratch = docs_dir / ".upload-tmp"
    assert not scratch.exists() or list(scratch.iterdir()) == []


def test_within_budget_is_parsed(doc_client: TestClient, docs_dir: Path, tmp_path: Path) -> None:
    content = _native_pdf(tmp_path / "n.pdf", 30)
    resp = _post(doc_client, content, "DOC-5002", budget_seconds="30")
    assert resp.status_code == 200
    assert (docs_dir / "General" / "DOC-5002" / "manifest.json").exists()


def test_no_budget_means_no_refusal(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path
) -> None:
    """The background REST leg exists to absorb exactly what the MCP leg refuses.

    A caller that declares no budget must not be refused — otherwise the
    execution mechanism would reject the work the main path is for.
    """
    content = _scanned_pdf(tmp_path / "s.pdf", 120)
    resp = _post(doc_client, content, "DOC-5003")
    assert resp.status_code != 422
    assert (docs_dir / "General" / "DOC-5003").exists()


def test_same_document_refused_or_accepted_by_who_is_asking(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path
) -> None:
    """Per-caller, not per-document (IRP §2.1, Harness §6.1)."""
    content = _scanned_pdf(tmp_path / "s.pdf", 120)
    assert _post(doc_client, content, "DOC-5004", budget_seconds="30").status_code == 422
    assert _post(doc_client, content, "DOC-5005", budget_seconds="600").status_code == 200


def test_unknown_cost_is_never_refused(
    doc_client: TestClient, docs_dir: Path
) -> None:
    """No estimate means unknown; refusing on a guess would be worse than a timeout."""
    resp = doc_client.post(
        "/parse",
        files={"file": ("page.html", b"<h1>T</h1><p>Body.</p>", "text/html")},
        data={"doc_id": "DOC-5006", "summary_mode": "off", "budget_seconds": "0.001"},
    )
    assert resp.status_code == 200


def test_estimate_agrees_with_the_planner_on_sparse_text(tmp_path: Path) -> None:
    """The estimate must be about the work the parse will actually do.

    A page with a little text is still OCR'd — the real predicate is
    _should_ocr at OCR_THRESHOLD (50), not "is the page blank". The text here is
    deliberately 33 characters: enough that a lower private threshold would call
    the page free, still under the one the parse actually applies.
    """
    import fitz
    from mantisfetch_docreader.pdf_planning import _should_ocr

    sparse = "Page 3 of 40 — continued overleaf"
    assert 20 < len(sparse) < dr.OCR_THRESHOLD, "must sit inside the divergence band"

    path = tmp_path / "sparse.pdf"
    doc = fitz.open()
    for _ in range(10):
        page = doc.new_page()
        page.insert_text((72, 72), sparse)
    doc.save(str(path))
    doc.close()

    with fitz.open(str(path)) as opened:
        planner_says = sum(1 for p in opened if _should_ocr(p, p.get_text(), dr.OCR_THRESHOLD))

    estimate = dr._estimate_parse_seconds(path, ".pdf")
    assert estimate["ocr_pages"] == planner_says == 10


def test_force_ocr_is_estimated_as_every_page(tmp_path: Path) -> None:
    """force_ocr OCRs the whole document however good its text layer is."""
    path = tmp_path / "native.pdf"
    _native_pdf(path, 40)

    assert dr._estimate_parse_seconds(path, ".pdf")["ocr_pages"] == 0
    forced = dr._estimate_parse_seconds(path, ".pdf", force_ocr=True)
    assert forced["ocr_pages"] == 40
    assert forced["estimated_seconds"] > 0


def test_explicit_ocr_pages_are_estimated(tmp_path: Path) -> None:
    path = tmp_path / "native.pdf"
    _native_pdf(path, 40)
    assert dr._estimate_parse_seconds(path, ".pdf", ocr_pages_spec="1-5")["ocr_pages"] == 5


def test_force_ocr_over_budget_is_refused(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path
) -> None:
    """The document is cheap; the request is not."""
    content = _native_pdf(tmp_path / "n.pdf", 40)
    assert _post(doc_client, content, "DOC-5007", budget_seconds="600").status_code == 200
    resp = _post(doc_client, content, "DOC-5008", budget_seconds="10", force_ocr="true")
    assert resp.status_code == 422
    assert resp.json()["detail"]["ocr_pages"] == 40


def test_a_declared_budget_takes_summarization_out_of_the_call(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise "estimate <= budget" would describe a fraction of the work.

    Summarization is an LLM round-trip that cannot be estimated, so a call that
    declared a deadline gets it off the critical path rather than inside it.
    """
    calls: list[str] = []
    monkeypatch.setattr(dr, "_generate_deferred_summary", lambda *a, **k: calls.append("deferred"))

    def sync_summary(*args, **kwargs):
        calls.append("sync")
        return ("digest", "brief", None)

    monkeypatch.setattr(dr, "generate_summaries", sync_summary)

    content = _native_pdf(tmp_path / "n.pdf", 3)
    resp = _post(
        doc_client, content, "DOC-5009", budget_seconds="600", summary_mode="sync",
        generate_summary="true",
    )
    assert resp.status_code == 200
    assert "sync" not in calls, "summarization ran inside a budgeted call"
    assert "deferred" in calls


def test_without_a_budget_a_sync_summary_stays_sync(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the downgrade is caused by the budget, not applied to everyone."""
    calls: list[str] = []
    monkeypatch.setattr(dr, "_generate_deferred_summary", lambda *a, **k: calls.append("deferred"))

    def sync_summary(*args, **kwargs):
        calls.append("sync")
        return ("digest", "brief", None)

    monkeypatch.setattr(dr, "generate_summaries", sync_summary)

    content = _native_pdf(tmp_path / "n.pdf", 3)
    resp = _post(doc_client, content, "DOC-5010", summary_mode="sync", generate_summary="true")
    assert resp.status_code == 200
    assert "sync" in calls


def test_mcp_doc_parse_declares_a_budget() -> None:
    """The MCP leg cannot see its client's timeout, so it sends its own."""
    import mantisfetch_mcp as mm

    assert mm._PARSE_BUDGET_SEC > 0
    assert mm._PARSE_BUDGET_SEC < 60, "must land inside the 60s agentd cap, not on it"

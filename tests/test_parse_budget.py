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


def test_full_mode_estimates_every_page_as_llm_ocr(tmp_path: Path) -> None:
    """parse_mode=full routes every non-blank page to the LLM, text layer or not."""
    path = tmp_path / "native.pdf"
    _native_pdf(path, 40)

    assert dr._estimate_parse_seconds(path, ".pdf")["llm_ocr_pages"] == 0
    full = dr._estimate_parse_seconds(path, ".pdf", parse_mode="full")
    assert full["llm_ocr_pages"] == 40
    assert full["estimated_seconds"] > 0


def test_llm_pages_cost_more_than_local_ones(tmp_path: Path) -> None:
    """Counting pages without the engine would be counting the wrong thing."""
    scan = tmp_path / "scan.pdf"
    _scanned_pdf(scan, 12)
    native = tmp_path / "native.pdf"
    _native_pdf(native, 12)

    local = dr._estimate_parse_seconds(scan, ".pdf")  # 12 pages, local OCR
    llm = dr._estimate_parse_seconds(native, ".pdf", force_ocr=True, concurrency=1)
    assert local["local_ocr_pages"] == llm["llm_ocr_pages"] == 12
    assert llm["estimated_seconds"] > local["estimated_seconds"]


def test_llm_pages_are_batched_by_concurrency(tmp_path: Path) -> None:
    path = tmp_path / "native.pdf"
    _native_pdf(path, 12)
    serial = dr._estimate_parse_seconds(path, ".pdf", force_ocr=True, concurrency=1)
    parallel = dr._estimate_parse_seconds(path, ".pdf", force_ocr=True, concurrency=4)
    assert parallel["estimated_seconds"] < serial["estimated_seconds"]


@pytest.mark.parametrize("source", ["form", "metadata", "env"])
def test_full_mode_is_honoured_from_every_source(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    """The mode reaches the parse from three places; a preflight that reads only
    the form field would under-estimate on the other two."""
    content = _native_pdf(tmp_path / "n.pdf", 40)
    extra: dict[str, str] = {}
    if source == "form":
        extra["parse_mode"] = "full"
    elif source == "metadata":
        extra["metadata"] = '{"parse_mode": "full"}'
    else:
        monkeypatch.setenv("MANTISFETCH_PDF_PARSE_MODE", "full")

    resp = _post(doc_client, content, f"DOC-511{source[0]}", budget_seconds="10", **extra)
    assert resp.status_code == 422, f"{source}-sourced full mode was not estimated"
    assert resp.json()["detail"]["llm_ocr_pages"] == 40


def test_effective_parse_plan_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANTISFETCH_PDF_PARSE_MODE", "fast")
    assert dr._effective_parse_plan("full", None)[0] == "full"
    assert dr._effective_parse_plan(None, '{"parse_mode": "full"}')[0] == "full"
    assert dr._effective_parse_plan(None, None)[0] == "fast"
    # a malformed blob must not break the preflight; it is rejected later
    assert dr._effective_parse_plan(None, "{not json")[0] == "fast"


def _profile_defaulting_to_full():
    """A real profile, rebuilt with an upgrade policy that chooses full mode.

    No shipped profile defaults to full, and the dataclasses are frozen, so the
    only honest way to exercise this path is to rebuild a genuine one.
    """
    import dataclasses

    from mantisfetch_docreader.profiles import _load_document_profile

    real = _load_document_profile("contract_cn", None)
    return dataclasses.replace(
        real, upgrade_policy=dataclasses.replace(real.upgrade_policy, default_mode="full")
    )


@pytest.mark.parametrize("source", ["form", "metadata", "env"])
def test_profile_default_mode_full_is_estimated(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    """With no explicit mode, the profile supplies one — and it can be full.

    The profile name itself reaches the request three ways, so a preflight that
    resolved the mode by hand would miss whichever one it forgot. It now loads
    the profile and asks _resolve_pdf_parse_mode, the same resolver the parse uses.
    """
    asked: list[tuple] = []

    def fake_loader(name, config):
        asked.append((name, config))
        return _profile_defaulting_to_full()

    monkeypatch.setattr(dr, "_load_document_profile", fake_loader)

    extra: dict[str, str] = {}
    if source == "form":
        extra["document_profile"] = "contract_cn"
    elif source == "metadata":
        extra["metadata"] = '{"document_profile": "contract_cn"}'
    else:
        monkeypatch.setenv("MANTISFETCH_FIELD_OCR_PROFILE", "contract_cn")

    content = _native_pdf(tmp_path / "n.pdf", 40)
    resp = _post(doc_client, content, f"DOC-512{source[0]}", budget_seconds="10", **extra)

    assert resp.status_code == 422, f"{source}-sourced profile default_mode=full was not estimated"
    assert resp.json()["detail"]["llm_ocr_pages"] == 40
    assert ("contract_cn", None) in asked, f"the profile was not consulted: {asked}"


def test_field_ocr_config_also_selects_the_profile(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """field_ocr_config picks a profile too, and carries its default mode."""
    asked: list[tuple] = []

    def fake_loader(name, config):
        asked.append((name, config))
        return _profile_defaulting_to_full()

    monkeypatch.setattr(dr, "_load_document_profile", fake_loader)
    content = _native_pdf(tmp_path / "n.pdf", 40)
    resp = _post(
        doc_client, content, "DOC-5130", budget_seconds="10", field_ocr_config="contract_cn"
    )
    assert resp.status_code == 422
    assert (None, "contract_cn") in asked


def test_an_unresolvable_profile_does_not_refuse(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No estimate means unknown, and unknown never refuses — the request fails
    later with a better message than a cost estimate could produce."""

    def broken_loader(name, config):
        raise RuntimeError("field OCR config not found: 'nope'")

    monkeypatch.setattr(dr, "_load_document_profile", broken_loader)
    assert dr._effective_parse_plan(None, None, "nope", None) == (None, None)

    # and through the endpoint: a document that WOULD be refused on a resolvable
    # plan must not be refused when the plan itself is unknown
    content = _scanned_pdf(tmp_path / "s.pdf", 120)
    resp = _post(doc_client, content, "DOC-5140", budget_seconds="30", document_profile="nope")
    assert resp.status_code != 422 or resp.json()["detail"].get("error") != "parse_budget_exceeded"


def test_profile_region_ocr_counts_against_the_budget(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path
) -> None:
    """The shipped contract_cn profile adds serial region LLM calls in accurate
    mode, which the page counts alone do not see.

    Its cover group is pinned to pages 1-2 with no alias gate, so a scanned
    document under this profile pays two region calls on top of page OCR. A
    20-page scan sits just under the MCP budget on pages alone and over it once
    the region work is counted — which is exactly the case that would otherwise
    slip through and time out.
    """
    content = _scanned_pdf(tmp_path / "s.pdf", 20)

    pages_only = dr._estimate_parse_seconds(tmp_path / "s.pdf", ".pdf")
    assert pages_only["estimated_seconds"] < 45, "premise: pages alone fit the budget"

    resp = _post(
        doc_client, content, "DOC-5150", budget_seconds="45", document_profile="contract_cn"
    )
    assert resp.status_code == 422, "region OCR was not counted against the budget"
    detail = resp.json()["detail"]
    # 2 from the group pinned to pages 1-2, plus the upper bound for the
    # alias-gated group, which has no page scope and so could match any page.
    assert detail["region_ocr_calls"] == 2 + 20
    assert detail["estimated_seconds"] > 45


def test_alias_gated_regions_are_counted_at_their_upper_bound(
    doc_client: TestClient, docs_dir: Path, tmp_path: Path
) -> None:
    """contract_cn also has a group with no page scope, gated on an alias.

    Which pages it matches depends on their text, which the preflight has not
    read — but the gate can only *reduce* the match, so every page is a true
    upper bound. Counting the bound is what makes the comparison sound: a number
    that excludes work which might happen would let exactly this document
    through and time out. Reporting the shortfall in the response instead would
    put the caveat somewhere the budget check never looks.
    """
    import fitz
    from mantisfetch_docreader.profiles import _load_document_profile

    profile = _load_document_profile("contract_cn", None)
    alias_group = next(g for g in profile.groups if g.crop and not g.page_scope and g.aliases)
    alias = alias_group.aliases[0]

    # Sparse pages that each carry the alias: OCR'd (under OCR_THRESHOLD) *and*
    # eligible for the alias-gated region group.
    path = tmp_path / "alias.pdf"
    doc = fitz.open()
    for _ in range(10):
        doc.new_page().insert_text((72, 72), alias)
    doc.save(str(path))
    doc.close()

    estimate = dr._estimate_parse_seconds(path, ".pdf", parse_mode="accurate", profile=profile)
    assert estimate["region_ocr_calls"] == 12, "2 pinned to pages 1-2 + 10 alias-eligible"

    resp = _post(
        doc_client, path.read_bytes(), "DOC-5160", budget_seconds="45",
        document_profile="contract_cn",
    )
    assert resp.status_code == 422, "a document whose region work blows the budget was let through"
    assert resp.json()["detail"]["region_ocr_calls"] == 12


def test_mcp_doc_parse_declares_a_budget() -> None:
    """The MCP leg cannot see its client's timeout, so it sends its own."""
    import mantisfetch_mcp as mm

    assert mm._PARSE_BUDGET_SEC > 0
    assert mm._PARSE_BUDGET_SEC < 60, "must land inside the 60s agentd cap, not on it"

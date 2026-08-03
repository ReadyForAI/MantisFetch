"""A failed parse must leave a record, not an anonymous empty directory (#209).

SharedSpecs IRP 20260801 §3.6 rules the shape: record rather than delete. The
four on-disk states a caller may probe — never submitted / in progress / failed /
succeeded — stay distinguishable only if the failure keeps a trace of its own,
so cleaning the directory away would collapse "failed" into "never happened".
"""

from __future__ import annotations

import json
from pathlib import Path

import mantisfetch_docreader as dr
import pytest
from starlette.testclient import TestClient

import mantisfetch_common.storage as cs

MARKER = dr.PARSE_FAILURE_MARKER
BROKEN_PDF = b"%PDF-1.4 not actually a pdf"
GOOD_HTML = b"<h1>Title</h1><p>Body text worth keeping.</p>"


@pytest.fixture()
def docs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


@pytest.fixture()
def doc_client() -> TestClient:
    return TestClient(dr.app, raise_server_exceptions=False)


def _post(client: TestClient, content: bytes, name: str, doc_id: str, **extra: str):
    return client.post(
        "/parse",
        files={"file": (name, content, "application/octet-stream")},
        data={"doc_id": doc_id, "summary_mode": "off", **extra},
    )


def test_failed_parse_leaves_a_marker(doc_client: TestClient, docs_dir: Path) -> None:
    resp = _post(doc_client, BROKEN_PDF, "bad.pdf", "DOC-4001")
    assert resp.status_code == 500

    marker = docs_dir / "General" / "DOC-4001" / MARKER
    assert marker.exists(), "a failed parse still leaves an anonymous empty directory"
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert record["doc_id"] == "DOC-4001"
    assert record["phase"] == "parse"
    assert record["error"]
    assert record["failed_at"].endswith("Z")


def test_successful_parse_leaves_no_marker(doc_client: TestClient, docs_dir: Path) -> None:
    resp = _post(doc_client, GOOD_HTML, "ok.html", "DOC-4002")
    assert resp.status_code == 200
    assert not (docs_dir / "General" / "DOC-4002" / MARKER).exists()


def test_a_later_success_clears_the_marker(doc_client: TestClient, docs_dir: Path) -> None:
    """A retry that works must stop reporting the previous attempt's failure."""
    assert _post(doc_client, BROKEN_PDF, "bad.pdf", "DOC-4003").status_code == 500
    marker = docs_dir / "General" / "DOC-4003" / MARKER
    assert marker.exists()

    assert _post(doc_client, GOOD_HTML, "ok.html", "DOC-4003", replace="true").status_code == 200
    assert not marker.exists()
    assert (docs_dir / "General" / "DOC-4003" / "manifest.json").exists()


def test_failed_replacement_does_not_flag_the_existing_document(
    doc_client: TestClient, docs_dir: Path
) -> None:
    """A failed replacement must not report a readable document as broken."""
    assert _post(doc_client, GOOD_HTML, "ok.html", "DOC-4004").status_code == 200
    doc = docs_dir / "General" / "DOC-4004"
    before = (doc / "manifest.json").read_text(encoding="utf-8")

    assert _post(doc_client, BROKEN_PDF, "bad.pdf", "DOC-4004", replace="true").status_code == 500

    assert not (doc / MARKER).exists(), "a live document was flagged as failed"
    # This particular failure happens before the write, so the document is also
    # untouched. That is not guaranteed for a failure *during* the write — see
    # test_replacement_that_fails_mid_write_is_not_flagged.
    assert (doc / "manifest.json").read_text(encoding="utf-8") == before


def test_new_document_that_fails_mid_write_is_recorded_not_left_looking_successful(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write path emits manifest.json before it updates the index.

    A failure in between used to leave a manifest with no index entry and no
    marker — on disk a success that nothing can find. Deciding by "is there a
    manifest now?" gets this exactly backwards, so the decision is made from
    whether the document existed *before* the request.
    """

    def boom(*args, **kwargs):
        raise RuntimeError("index write failed")

    monkeypatch.setattr(dr, "_update_doc_index", boom)
    assert _post(doc_client, GOOD_HTML, "ok.html", "DOC-4007").status_code == 500

    doc = docs_dir / "General" / "DOC-4007"
    assert (doc / MARKER).exists(), "a mid-write failure left no record"
    assert json.loads((doc / MARKER).read_text(encoding="utf-8"))["phase"] == "write"
    assert not (doc / "manifest.json").exists(), "still claims success but is unindexed"


def test_replacement_that_fails_mid_write_is_not_flagged(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same fault: an existing document must not be marked.

    It may still have been partially overwritten — the write path replaces in
    place and has no rollback. That is a pre-existing property of the write path
    rather than something this record introduces or can repair, so this test
    pins only what is in scope: the document is not reported as failed, and no
    marker is left for a doc_id that has a readable manifest.
    """
    assert _post(doc_client, GOOD_HTML, "ok.html", "DOC-4008").status_code == 200
    doc = docs_dir / "General" / "DOC-4008"

    def boom(*args, **kwargs):
        raise RuntimeError("index write failed")

    monkeypatch.setattr(dr, "_update_doc_index", boom)
    assert _post(doc_client, GOOD_HTML, "ok2.html", "DOC-4008", replace="true").status_code == 500

    assert not (doc / MARKER).exists()
    assert (doc / "manifest.json").exists()


def test_the_four_states_stay_distinguishable(doc_client: TestClient, docs_dir: Path) -> None:
    """The property the IRP decision leans on (§3.2), pinned on disk.

    'In progress' is covered by tests/test_parse_cancellation_safety.py, which
    observes a live parse; here the point is that 'failed' no longer looks like
    either of its neighbours.
    """
    assert _post(doc_client, GOOD_HTML, "ok.html", "DOC-4005").status_code == 200
    assert _post(doc_client, BROKEN_PDF, "bad.pdf", "DOC-4006").status_code == 500

    succeeded = docs_dir / "General" / "DOC-4005"
    failed = docs_dir / "General" / "DOC-4006"
    never = docs_dir / "General" / "DOC-4099"

    assert (succeeded / "manifest.json").exists() and not (succeeded / MARKER).exists()
    assert (failed / MARKER).exists() and not (failed / "manifest.json").exists()
    assert not never.exists()
    # and the failed one is no longer an empty directory
    assert [p.name for p in failed.iterdir()] == [MARKER]

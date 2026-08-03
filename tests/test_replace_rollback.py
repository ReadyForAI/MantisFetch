"""A replacement that fails leaves the document it was replacing intact (#212).

Rewrites replace in place — the generated directories are deleted before the new
ones are written, and the manifest lands before the index is updated. A failure
in between used to leave a document that was part old and part new, while the
caller was told the replace had failed.
"""

from __future__ import annotations

import json
from pathlib import Path

import mantisfetch_docreader as dr
import pytest
from starlette.testclient import TestClient

import mantisfetch_common.storage as cs

FIRST = b"<h1>Original</h1><p>Zarquonium bearings, part XQ-1.</p>"
SECOND = b"<h1>Replacement</h1><p>Different content entirely.</p>"


@pytest.fixture()
def docs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


@pytest.fixture()
def doc_client() -> TestClient:
    return TestClient(dr.app, raise_server_exceptions=False)


def _post(client: TestClient, content: bytes, doc_id: str, **extra):
    return client.post(
        "/parse",
        files={"file": ("doc.html", content, "text/html")},
        data={"doc_id": doc_id, "summary_mode": "off", **extra},
    )


def _snapshot(doc_dir: Path, *, skip: tuple[str, ...] = ()) -> dict[str, bytes]:
    return {
        rel: p.read_bytes()
        for p in sorted(doc_dir.rglob("*"))
        if p.is_file() and not (rel := str(p.relative_to(doc_dir))).startswith(skip or ("\0",))
    }


#: The uploaded file is stored during the parse, before the writer runs, so it is
#: already the replacement's by the time a rewrite can be rolled back. Restoring
#: it belongs to the handler, not here — see the note on #212.
_NOT_YET_ROLLED_BACK = ("source/",)


def test_a_failed_replacement_leaves_the_document_byte_identical(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _post(doc_client, FIRST, "DOC-6001").status_code == 200
    doc = docs_dir / "General" / "DOC-6001"
    before = _snapshot(doc, skip=_NOT_YET_ROLLED_BACK)
    assert "manifest.json" in before and "full.md" in before

    def boom(*args, **kwargs):
        raise RuntimeError("index write failed")

    monkeypatch.setattr(dr, "_update_doc_index", boom)
    assert _post(doc_client, SECOND, "DOC-6001", replace="true").status_code == 500

    assert _snapshot(doc, skip=_NOT_YET_ROLLED_BACK) == before, (
        "the document was left part old and part new"
    )


def test_a_failed_replacement_does_not_leave_rollback_scaffolding(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _post(doc_client, FIRST, "DOC-6002").status_code == 200
    doc = docs_dir / "General" / "DOC-6002"

    monkeypatch.setattr(dr, "_update_doc_index", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert _post(doc_client, SECOND, "DOC-6002", replace="true").status_code == 500

    assert not (doc / dr._ROLLBACK_DIR).exists(), "rollback holdings were left behind"


def test_a_successful_replacement_still_replaces(
    doc_client: TestClient, docs_dir: Path
) -> None:
    """Control: the guard must not turn a working rewrite into a no-op."""
    assert _post(doc_client, FIRST, "DOC-6003").status_code == 200
    doc = docs_dir / "General" / "DOC-6003"
    assert b"Zarquonium" in (doc / "full.md").read_bytes()

    assert _post(doc_client, SECOND, "DOC-6003", replace="true").status_code == 200
    full = (doc / "full.md").read_bytes()
    assert b"Different content" in full
    assert b"Zarquonium" not in full, "the old content survived a successful replace"
    assert not (doc / dr._ROLLBACK_DIR).exists()


def test_the_index_still_points_at_the_old_document_after_a_failed_replace(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disk and index have to agree: both stay on the document that is there."""
    assert _post(doc_client, FIRST, "DOC-6004").status_code == 200
    index_before = (docs_dir / "doc-index.json").read_text(encoding="utf-8")

    monkeypatch.setattr(
        dr, "_update_doc_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert _post(doc_client, SECOND, "DOC-6004", replace="true").status_code == 500

    assert (docs_dir / "doc-index.json").read_text(encoding="utf-8") == index_before
    entry = next(
        e
        for e in json.loads(index_before)["documents"]
        if e["id"] == "DOC-6004"
    )
    assert entry["filename"] == "doc.html"


def test_untouched_artifacts_survive_a_rollback(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """source/ and .cache/ are not regenerated, so a rollback must not disturb them."""
    assert _post(doc_client, FIRST, "DOC-6005").status_code == 200
    doc = docs_dir / "General" / "DOC-6005"
    cache = doc / ".cache"
    cache.mkdir(exist_ok=True)
    (cache / "ocr_p0001.abc.txt").write_text("cached page", encoding="utf-8")
    source_before = _snapshot(doc / "source") if (doc / "source").exists() else {}

    monkeypatch.setattr(
        dr, "_update_doc_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert _post(doc_client, SECOND, "DOC-6005", replace="true").status_code == 500

    assert (cache / "ocr_p0001.abc.txt").read_text(encoding="utf-8") == "cached page"



def test_the_stored_source_is_a_known_gap_not_a_silent_one(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The uploaded file is stored during the parse, before any writer runs.

    By the time a rewrite can be rolled back it is already the replacement's, so
    a failed replace leaves the old parsed content beside the new source — and
    the restored manifest's source sha256 describes the file that is no longer
    there. Restoring it belongs to the parse handler, which owns that step; this
    pins the current boundary so it is visible rather than assumed away.
    """
    assert _post(doc_client, FIRST, "DOC-6006").status_code == 200
    doc = docs_dir / "General" / "DOC-6006"

    monkeypatch.setattr(
        dr, "_update_doc_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert _post(doc_client, SECOND, "DOC-6006", replace="true").status_code == 500

    assert b"Zarquonium" in (doc / "full.md").read_bytes(), "parsed content rolled back"
    stored = next((doc / "source").iterdir()).read_bytes()
    assert stored == SECOND, "if this now equals FIRST the gap is closed — widen the guard"


def test_the_sync_summary_writer_rolls_back_too(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There are two writers, and summary_mode decides which one runs.

    The tests above all go through the extract-only path; without this one the
    guard could be missing from write_output entirely and they would still pass.
    """
    monkeypatch.setattr(dr, "generate_summaries", lambda *a, **k: ("digest", "brief", None))

    assert _post(doc_client, FIRST, "DOC-6007", summary_mode="sync").status_code == 200
    doc = docs_dir / "General" / "DOC-6007"
    before = _snapshot(doc, skip=_NOT_YET_ROLLED_BACK)
    assert b"digest" in (doc / "digest.md").read_bytes()

    monkeypatch.setattr(
        dr, "_update_doc_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert _post(
        doc_client, SECOND, "DOC-6007", summary_mode="sync", replace="true"
    ).status_code == 500

    assert _snapshot(doc, skip=_NOT_YET_ROLLED_BACK) == before

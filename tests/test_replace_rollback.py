"""A replacement that fails leaves the document it was replacing intact (#212).

Rewrites replace in place — the generated directories are deleted before the new
ones are written, and the manifest lands before the index is updated. A failure
in between used to leave a document that was part old and part new, while the
caller was told the replace had failed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import mantisfetch_docreader as dr
import pytest
from starlette.testclient import TestClient

import mantisfetch_common.storage as cs
from mantisfetch_common.doc_index_store import list_documents, search_fts, upsert_fts

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


def _snapshot(doc_dir: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(doc_dir)): p.read_bytes()
        for p in sorted(doc_dir.rglob("*"))
        if p.is_file()
    }


def test_a_failed_replacement_leaves_the_document_byte_identical(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _post(doc_client, FIRST, "DOC-6001").status_code == 200
    doc = docs_dir / "General" / "DOC-6001"
    before = _snapshot(doc)
    assert "manifest.json" in before and "full.md" in before

    def boom(*args, **kwargs):
        raise RuntimeError("index write failed")

    monkeypatch.setattr(dr, "_update_doc_index", boom)
    assert _post(doc_client, SECOND, "DOC-6001", replace="true").status_code == 500

    assert _snapshot(doc) == before, "the document was left part old and part new"


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
    """.cache/ holds OCR pages nothing regenerates, so a rollback must leave it."""
    assert _post(doc_client, FIRST, "DOC-6005").status_code == 200
    doc = docs_dir / "General" / "DOC-6005"
    cache = doc / ".cache"
    cache.mkdir(exist_ok=True)
    (cache / "ocr_p0001.abc.txt").write_text("cached page", encoding="utf-8")

    monkeypatch.setattr(
        dr, "_update_doc_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert _post(doc_client, SECOND, "DOC-6005", replace="true").status_code == 500

    assert (cache / "ocr_p0001.abc.txt").read_text(encoding="utf-8") == "cached page"


def test_the_stored_source_goes_back_and_still_matches_the_manifest(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upload is stored before the writer runs, so it needs restoring too.

    Without that the caller is told the replace failed while downloading the
    source hands back the replacement's file, and the restored manifest's
    source_sha256 describes bytes that are no longer on disk.
    """
    assert _post(doc_client, FIRST, "DOC-6006").status_code == 200
    doc = docs_dir / "General" / "DOC-6006"

    monkeypatch.setattr(
        dr, "_update_doc_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert _post(doc_client, SECOND, "DOC-6006", replace="true").status_code == 500

    stored = next((doc / "source").iterdir())
    assert stored.read_bytes() == FIRST, "the replacement's source outlived the failed replace"
    manifest = json.loads((doc / "manifest.json").read_text(encoding="utf-8"))
    assert (
        manifest["provenance"]["source_sha256"]
        == hashlib.sha256(stored.read_bytes()).hexdigest()
    ), "the restored manifest describes a file that is not there"
    assert not (doc / dr._SOURCE_ROLLBACK_DIR).exists()


def test_a_successful_replacement_keeps_the_new_source_and_drops_the_stash(
    doc_client: TestClient, docs_dir: Path
) -> None:
    """Control for the stash: it must not survive or shadow a working replace."""
    assert _post(doc_client, FIRST, "DOC-6008").status_code == 200
    doc = docs_dir / "General" / "DOC-6008"

    assert _post(doc_client, SECOND, "DOC-6008", replace="true").status_code == 200

    assert next((doc / "source").iterdir()).read_bytes() == SECOND
    assert not (doc / dr._SOURCE_ROLLBACK_DIR).exists()


def test_a_failed_replacement_leaves_the_old_text_searchable(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full-text search lives in a library-wide table, not this directory.

    The writer pushes the replacement's text into it before the manifest lands,
    so a rollback that stopped at the filesystem would leave the old words on
    disk and the new ones matching this doc_id.
    """
    assert _post(doc_client, FIRST, "DOC-6009").status_code == 200
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-6009"]

    monkeypatch.setattr(
        dr, "_update_doc_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert _post(doc_client, SECOND, "DOC-6009", replace="true").status_code == 500

    assert search_fts(docs_dir, "Zarquonium") == ["DOC-6009"], "the old text stopped matching"
    assert search_fts(docs_dir, "Different content entirely") == [], (
        "the failed replacement's text is still searchable"
    )


def test_a_backup_that_fails_halfway_still_restores_what_it_moved(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging the rollback can fail too — after it has already moved something.

    A directory moves aside, then the copy after it hits a full disk. Without
    recovery around the staging itself the guard gives up before the rewrite even
    starts, having already taken the document apart.
    """
    assert _post(doc_client, FIRST, "DOC-6010").status_code == 200
    doc = docs_dir / "General" / "DOC-6010"
    before = _snapshot(doc)

    real_copy2 = dr.shutil.copy2

    def fail_on_manifest(src, dst, *a, **k):
        if Path(src).name == "manifest.json":
            raise OSError(28, "No space left on device")
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(dr.shutil, "copy2", fail_on_manifest)
    assert _post(doc_client, SECOND, "DOC-6010", replace="true").status_code == 500

    assert (doc / "sections").is_dir(), "a moved-aside directory was never put back"
    assert _snapshot(doc) == before
    assert not (doc / dr._ROLLBACK_DIR).exists(), "rollback holdings were left behind"


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
    before = _snapshot(doc)
    assert b"digest" in (doc / "digest.md").read_bytes()

    monkeypatch.setattr(
        dr, "_update_doc_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert _post(
        doc_client, SECOND, "DOC-6007", summary_mode="sync", replace="true"
    ).status_code == 500

    assert _snapshot(doc) == before


def test_a_failed_replacement_keeps_a_document_that_had_no_indexed_text(
    doc_client: TestClient, docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No FTS row is not the same fact as no document.

    A document can be in the library with nothing indexed under it — an empty
    parse, or an FTS read the rollback could not complete. Restoring "nothing"
    has to mean an empty FTS row, not removing the document.
    """
    assert _post(doc_client, FIRST, "DOC-6011").status_code == 200
    upsert_fts(docs_dir, "DOC-6011", "")  # the row a document like this would have

    monkeypatch.setattr(
        dr, "_update_doc_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert _post(doc_client, SECOND, "DOC-6011", replace="true").status_code == 500

    assert [d["id"] for d in list_documents(docs_dir)] == ["DOC-6011"], (
        "the failed replace dropped the document from the library index"
    )
    assert search_fts(docs_dir, "Different content entirely") == []


def test_a_stale_deferred_write_leaves_every_artifact_of_the_newer_parse(
    doc_client: TestClient, docs_dir: Path
) -> None:
    """The rollback guard must not turn a declined write into a deletion.

    A deferred-summary thread that finds a newer parse on disk writes nothing.
    Staging the rollback is itself a mutation — sections/ moves aside — so a
    writer that then declines would leave through the success path and drop the
    holdings along with the newer parse's sections.
    """
    assert _post(doc_client, FIRST, "DOC-6012").status_code == 200
    doc = docs_dir / "General" / "DOC-6012"
    before = _snapshot(doc)
    assert any(name.startswith("sections/") for name in before)

    stale = dr.ParsedDocument(
        filename="doc.html",
        file_type="html",
        total_pages=1,
        pages=[],
        sections=[
            dr.Section(
                index=1, title="Stale", level=1, text="stale text",
                page_range="1-1", sid="s_stale", summary="",
            )
        ],
        ocr_page_count=0,
        table_count=0,
    )
    dr.write_output(
        "DOC-6012", stale, "STALE_DIGEST", "STALE_BRIEF", docs_dir,
        source="upload", content_type="General", guard_stale_generation=True,
    )

    assert _snapshot(doc) == before, "the declined write removed the newer parse's artifacts"

"""One index is authoritative, and a failed write is a failed write.

Before this, the library had two authorities: docreader read SQLite and fell
back to JSON, browser read only `doc-index.json`, and five writers used three
different orders. A SQLite failure was swallowed and left a JSON-only record —
which the *next* successful write erased, because `export_json` rewrites JSON
from SQLite. The same shape ran in reverse for deletes: a JSON-only delete came
back the next time anything exported.

That is the failure this file pins. The rule now: SQLite is the commit point,
JSON is its export, and a write that cannot commit says so instead of leaving a
record that a later write will quietly undo.
"""

import json

import pytest


def _meta(i: int) -> dict:
    return {
        "doc_id": f"DOC-{i}",
        "filename": f"f{i}.txt",
        "file_type": "txt",
        "total_pages": 1,
        "section_count": 1,
        "created_at": "2026-09-07T00:00:00Z",
    }


@pytest.fixture()
def docs(monkeypatch, tmp_path):
    import mantisfetch_common.storage as cs

    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


def _ids(docs_dir):
    return [e["id"] for e in json.loads((docs_dir / "doc-index.json").read_text())["documents"]]


def _fail_upsert_for(monkeypatch, doc_id):
    import mantisfetch_common.doc_index_store as dis

    real = dis.upsert_document

    def guarded(d, entry):
        if entry.get("id") == doc_id:
            raise OSError("database is locked")
        return real(d, entry)

    monkeypatch.setattr(dis, "upsert_document", guarded)


# ── a write that cannot commit is not a write ────────────────────────────────────
def test_an_index_write_that_cannot_commit_raises(docs, monkeypatch) -> None:
    """It used to be swallowed, and the document then existed on disk while being
    invisible to every query — the one state a caller cannot detect or repair."""
    from mantisfetch_docreader.storage import _update_doc_index

    _update_doc_index(docs, _meta(1), "d1")
    _fail_upsert_for(monkeypatch, "DOC-2")

    with pytest.raises(OSError):
        _update_doc_index(docs, _meta(2), "d2")


def test_a_failed_write_leaves_no_record_for_a_later_export_to_erase(docs, monkeypatch) -> None:
    """The loss itself: JSON kept DOC-2, SQLite did not, and the next successful
    write rewrote JSON from SQLite. Whichever way the failure is reported, the
    two stores must not disagree about DOC-2 afterwards."""
    from mantisfetch_docreader.storage import _load_doc_index, _update_doc_index

    _update_doc_index(docs, _meta(1), "d1")
    _fail_upsert_for(monkeypatch, "DOC-2")
    with pytest.raises(OSError):
        _update_doc_index(docs, _meta(2), "d2")

    monkeypatch.undo()
    _update_doc_index(docs, _meta(3), "d3")

    assert _ids(docs) == [e["id"] for e in _load_doc_index(docs)], "JSON and SQLite disagree"
    assert "DOC-2" not in _ids(docs)


def test_a_delete_that_cannot_commit_does_not_come_back(docs, monkeypatch) -> None:
    """The same shape in reverse. A JSON-only delete looked like it worked, and
    the next export — from a SQLite that still held the row — resurrected the
    document."""
    from mantisfetch_docreader.storage import _update_doc_index

    import mantisfetch_common.doc_index_store as dis

    _update_doc_index(docs, _meta(1), "d1")
    _update_doc_index(docs, _meta(2), "d2")

    monkeypatch.setattr(
        dis, "delete_document", lambda d, i: (_ for _ in ()).throw(OSError("database is locked"))
    )
    doc_dir = docs / "General" / "DOC-2"
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "manifest.json").write_text("{}")

    from mantisfetch_docreader.storage import _delete_doc

    with pytest.raises(OSError):
        _delete_doc(docs, "DOC-2")

    monkeypatch.undo()
    _update_doc_index(docs, _meta(3), "d3")
    # Whatever the delete did, it must not be reported as done and then undone.
    assert "DOC-2" in _ids(docs), "the delete failed, so the document should still be there"


# ── both services read the same index ────────────────────────────────────────────
def test_the_browser_reads_the_same_index_docreader_writes(docs) -> None:
    """browser resolved cache hits from doc-index.json alone while docreader
    preferred SQLite. Two readers, two authorities: a document present in one and
    not the other behaved differently depending on which service asked."""
    import mantisfetch_browser as web
    from mantisfetch_docreader.storage import _update_doc_index

    _update_doc_index(docs, _meta(1), "d1")
    # Make JSON stale the way a partial write would: SQLite is the truth.
    (docs / "doc-index.json").write_text(json.dumps({"version": 2, "documents": []}))

    index = web._load_doc_index(docs)
    ids = [d.get("id") for d in (index or {}).get("documents", [])]
    assert ids == ["DOC-1"], f"browser read the stale JSON: {ids}"


# ── the capture path commits the same way ────────────────────────────────────────
def test_a_capture_whose_index_write_fails_does_not_leave_a_doomed_record(
    docs, monkeypatch
) -> None:
    """The browser had its own version of the same fallback: when the database
    refused, it wrote the entry to JSON alone. That record was invisible to
    docreader and erased by the next export from anywhere in the library."""
    import mantisfetch_browser as web
    from mantisfetch_docreader.storage import _update_doc_index

    _fail_upsert_for(monkeypatch, "WEB-9")
    with pytest.raises(OSError):
        web._persist_web_capture(
            "WEB-9",
            "https://example.com/x",
            "T",
            [{"h": "T", "t": "body", "sid": "s_001"}],
            "digest",
            [],
            "hash9",
            docs,
        )

    monkeypatch.undo()
    _update_doc_index(docs, _meta(1), "d1")
    assert "WEB-9" not in _ids(docs)


def test_a_tag_merge_that_cannot_commit_is_not_reported_as_merged(docs, monkeypatch) -> None:
    """Content-hash reuse merges the new caller's tags into the existing entry.
    Merging into the JSON export and writing it back put the merge exactly where
    the next export would overwrite it."""
    import mantisfetch_browser as web

    web._persist_web_capture(
        "WEB-8",
        "https://example.com/y",
        "T",
        [{"h": "T", "t": "body", "sid": "s_001"}],
        "digest",
        ["first"],
        "hash8",
        docs,
    )
    _fail_upsert_for(monkeypatch, "WEB-8")

    entry = next(
        e
        for e in json.loads((docs / "doc-index.json").read_text())["documents"]
        if e["id"] == "WEB-8"
    )
    with pytest.raises(OSError):
        web._merge_capture_tags_metadata(docs, entry, ["second"], None)

    monkeypatch.undo()
    stored = next(
        e
        for e in json.loads((docs / "doc-index.json").read_text())["documents"]
        if e["id"] == "WEB-8"
    )
    assert stored["tags"] == ["first"], "a merge that could not commit must not appear committed"


# ── what a *committed* write must survive (local Codex review of #255) ───────────
def test_a_failed_export_does_not_undo_a_committed_write(docs, monkeypatch) -> None:
    """The export is a derived view. Raising when it fails made the caller roll
    its files back around a row that was already committed — a replacement would
    end up with the old document on disk and the new one's metadata in the
    index, which is worse than a stale JSON file."""
    import mantisfetch_common.doc_index_store as dis
    from mantisfetch_docreader.storage import _load_doc_index, _update_doc_index

    _update_doc_index(docs, _meta(1), "d1")
    monkeypatch.setattr(
        dis, "export_json", lambda d, **kw: (_ for _ in ()).throw(OSError("disk full"))
    )

    _update_doc_index(docs, _meta(2), "d2")  # must not raise

    monkeypatch.undo()
    assert [e["id"] for e in _load_doc_index(docs)] == ["DOC-1", "DOC-2"]


def test_a_capture_that_cannot_commit_leaves_nothing_published(docs, monkeypatch) -> None:
    """A published directory with no index row is a document nothing can find
    and nothing will clean up; retries pile up more of them."""
    import mantisfetch_browser as web

    _fail_upsert_for(monkeypatch, "WEB-7")
    with pytest.raises(OSError):
        web._persist_web_capture(
            "WEB-7",
            "https://example.com/z",
            "T",
            [{"h": "T", "t": "body", "sid": "s_001"}],
            "digest",
            [],
            "hash7",
            docs,
        )

    assert not (docs / "General" / "WEB-7").exists()
    assert not list(docs.glob("General/*.rollback-capture"))


def test_a_failed_re_capture_keeps_the_document_it_was_replacing(docs, monkeypatch) -> None:
    """Publishing used to delete the previous version before the new one was
    committed, so a failing commit lost both."""
    import mantisfetch_browser as web

    def capture(digest, content_hash):
        web._persist_web_capture(
            "WEB-6",
            "https://example.com/w",
            "T",
            [{"h": "T", "t": "body", "sid": "s_001"}],
            digest,
            [],
            content_hash,
            docs,
        )

    capture("first digest", "hash-a")
    _fail_upsert_for(monkeypatch, "WEB-6")
    with pytest.raises(OSError):
        capture("second digest", "hash-b")

    monkeypatch.undo()
    assert (docs / "General" / "WEB-6" / "manifest.json").exists()
    assert "first digest" in (docs / "General" / "WEB-6" / "digest.md").read_text()
    assert not list(docs.glob("General/*.rollback-capture"))


def test_a_summary_claim_that_cannot_be_recorded_is_given_back(docs, monkeypatch) -> None:
    """The claim is what stops a second cache hit enqueueing a duplicate job.
    Left at "pending" with no worker behind it, every later request reads "one is
    already in flight" and none ever starts."""
    import mantisfetch_browser as web

    web._persist_web_capture(
        "WEB-5",
        "https://example.com/v",
        "T",
        [{"h": "T", "t": "body worth keeping", "sid": "s_001"}],
        "digest",
        [],
        "hash5",
        docs,
        summary_mode="off",
    )
    entry = next(
        e
        for e in json.loads((docs / "doc-index.json").read_text())["documents"]
        if e["id"] == "WEB-5"
    )
    _fail_upsert_for(monkeypatch, "WEB-5")

    status = web._resolve_cached_summary(entry, docs, "General", "defer")
    assert status != "pending", "a claim nobody is working on must not read as in flight"
    assert web._read_web_summary_status(docs / "General" / "WEB-5") == "failed"

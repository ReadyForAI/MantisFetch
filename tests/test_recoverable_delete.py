"""A delete either happens or leaves the document whole.

The delete used to remove the product directories first and the index row
second. When the row could not be deleted — a locked database — the caller got
an error, correctly, about a document whose files were already gone: the index
still listed it and every read of it failed. Retrying could only finish the
delete; nothing could bring the files back.

Now the products are renamed aside, the row is deleted, and only then are the
products removed. A failed commit renames them back. A process that dies in
between leaves a ``.deleting`` directory that the next start settles by asking
the index whether the delete committed.
"""

import asyncio
import json
import sqlite3

import pytest


@pytest.fixture()
def docs(monkeypatch, tmp_path):
    import mantisfetch_common.storage as cs

    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


def _add(docs_dir, doc_id, body="the body"):
    """A document the way the parse path leaves it: products, then the row."""
    from mantisfetch_docreader.storage import _update_doc_index

    doc_dir = docs_dir / "General" / doc_id
    doc_dir.mkdir(parents=True)
    (doc_dir / "full.md").write_text(body)
    (doc_dir / "manifest.json").write_text(json.dumps({"doc_id": doc_id}))
    _update_doc_index(
        docs_dir,
        {
            "doc_id": doc_id,
            "filename": f"{doc_id}.txt",
            "file_type": "txt",
            "total_pages": 1,
            "section_count": 1,
            "created_at": "2026-09-12T00:00:00Z",
        },
        "digest",
        content_type="General",
    )
    return doc_dir


def _indexed(docs_dir):
    import mantisfetch_common.doc_index_store as dis

    return {e["id"] for e in dis.list_documents(docs_dir)}


def _tombstones(docs_dir):
    return sorted(p.name for p in docs_dir.rglob("*.deleting"))


def _locked(*_a, **_k):
    raise sqlite3.OperationalError("database is locked")


def test_a_delete_that_cannot_commit_leaves_the_document_readable(client, docs, monkeypatch):
    """The report's repro, through the endpoint: the row stays and so do the
    files, so the document that is still listed can still be read."""
    import mantisfetch_common.doc_index_store as dis

    _add(docs, "DOC-001", body="still here")
    real_delete = dis.delete_document
    monkeypatch.setattr(dis, "delete_document", _locked)

    with pytest.raises(sqlite3.OperationalError):
        client.delete("/doc/library/DOC-001")

    assert "DOC-001" in _indexed(docs)
    resp = client.get("/doc/library/DOC-001/full")
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "still here"
    assert _tombstones(docs) == []

    # And the delete the caller retries goes through.
    monkeypatch.setattr(dis, "delete_document", real_delete)
    assert client.delete("/doc/library/DOC-001").json() == {"doc_id": "DOC-001", "deleted": True}
    assert "DOC-001" not in _indexed(docs)
    assert not (docs / "General" / "DOC-001").exists()
    assert _tombstones(docs) == []


def test_a_rename_that_fails_part_way_puts_back_what_it_moved(docs, monkeypatch):
    """Products in two layouts; the second rename fails. The first is not left
    stranded under its tombstone name."""
    from mantisfetch_docreader import storage

    doc_dir = _add(docs, "DOC-001")
    legacy = docs / "DOC-001"
    legacy.mkdir()
    (legacy / "old.md").write_text("legacy layout")

    real_replace = storage.os.replace

    def failing_replace(src, dst):
        if str(src) == str(legacy.resolve()) and str(dst).endswith(".deleting"):
            raise PermissionError("read-only")
        return real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", failing_replace)
    with pytest.raises(PermissionError):
        storage._delete_doc(docs, "DOC-001")
    monkeypatch.setattr(storage.os, "replace", real_replace)

    assert (doc_dir / "full.md").read_text() == "the body"
    assert (legacy / "old.md").read_text() == "legacy layout"
    assert "DOC-001" in _indexed(docs)
    assert _tombstones(docs) == []


def test_a_cleanup_that_fails_after_the_commit_is_still_a_delete(docs, monkeypatch, caplog):
    """The row is gone, so the document is gone. What is left on disk is not a
    reason to tell the caller otherwise — it is cleared at the next start."""
    from mantisfetch_docreader import storage

    _add(docs, "DOC-001")
    _add(docs, "DOC-002")
    real_rmtree = storage.shutil.rmtree

    def failing_rmtree(path, *a, **k):
        if str(path).endswith("DOC-001.deleting"):
            raise PermissionError("busy")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(storage.shutil, "rmtree", failing_rmtree)
    assert storage._delete_doc(docs, "DOC-001") is True
    monkeypatch.setattr(storage.shutil, "rmtree", real_rmtree)

    assert "DOC-001" not in _indexed(docs)
    assert not (docs / "General" / "DOC-001").exists()
    assert _tombstones(docs) == ["DOC-001.deleting"]
    assert "removed at the next start" in caplog.text

    assert storage._finish_interrupted_deletes(docs) == (0, 1)
    assert _tombstones(docs) == []
    assert (docs / "General" / "DOC-002" / "full.md").exists()


def test_the_next_start_settles_a_delete_the_process_died_inside(docs):
    """Three leftovers, three answers — and nothing else in the library moves.

    - renamed aside, never committed (the row is still there): put back
    - committed, never cleared (no row): cleared
    - a stale tombstone next to a live copy: the live copy wins
    """
    from mantisfetch_docreader import storage

    uncommitted = _add(docs, "DOC-001", body="uncommitted")
    uncommitted.rename(uncommitted.with_name("DOC-001.deleting"))

    committed = docs / "General" / "DOC-002.deleting"
    committed.mkdir()
    (committed / "full.md").write_text("committed")

    live = _add(docs, "DOC-003", body="live")
    stale = docs / "General" / "DOC-003.deleting"
    stale.mkdir()
    (stale / "full.md").write_text("stale")

    bystander = _add(docs, "DOC-004", body="bystander")

    assert storage._finish_interrupted_deletes(docs) == (1, 2)

    assert (uncommitted / "full.md").read_text() == "uncommitted"
    assert (live / "full.md").read_text() == "live"
    assert (bystander / "full.md").read_text() == "bystander"
    assert _tombstones(docs) == []
    assert _indexed(docs) == {"DOC-001", "DOC-003", "DOC-004"}


def test_the_sweep_touches_nothing_when_it_cannot_read_the_index(docs, monkeypatch):
    """An unreadable index is not an empty one. Treating it as empty would clear
    every uncommitted delete's products — the documents the index still lists."""
    from mantisfetch_docreader import storage

    import mantisfetch_common.doc_index_store as dis

    doc_dir = _add(docs, "DOC-001")
    doc_dir.rename(doc_dir.with_name("DOC-001.deleting"))
    monkeypatch.setattr(dis, "list_documents", _locked)

    assert storage._finish_interrupted_deletes(docs) == (0, 0)
    assert _tombstones(docs) == ["DOC-001.deleting"]


def test_startup_runs_the_sweep(docs):
    """The sweep is only a fix if something calls it."""
    import mantisfetch_docreader as dr

    doc_dir = _add(docs, "DOC-001", body="restored at startup")
    doc_dir.rename(doc_dir.with_name("DOC-001.deleting"))

    async def _start_and_stop() -> None:
        async with dr.lifespan(dr.app):
            pass

    asyncio.run(_start_and_stop())
    assert (doc_dir / "full.md").read_text() == "restored at startup"
    assert _tombstones(docs) == []

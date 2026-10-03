"""B3: SQLite document index + FTS."""

import json
import sqlite3
from pathlib import Path

import pytest

from mantisfetch_common import doc_index_store as dis
from mantisfetch_common.search_cache import write_search_cache


def test_sqlite_upsert_list_delete(tmp_path: Path):
    docs_dir = tmp_path
    entry = {
        "id": "DOC-1",
        "filename": "a.pdf",
        "file_type": "pdf",
        "content_type": "General",
        "source": "upload",
        "digest": "hello",
        "created_at": "2026-01-01T00:00:00Z",
        "content_hash": "sha256:x",
    }
    dis.upsert_document(docs_dir, entry)
    docs = dis.list_documents(docs_dir)
    assert len(docs) == 1 and docs[0]["id"] == "DOC-1"
    dis.export_json(docs_dir, last_updated="t")
    assert (docs_dir / "doc-index.json").exists()
    dis.delete_document(docs_dir, "DOC-1")
    assert dis.list_documents(docs_dir) == []


def test_fts_search(tmp_path: Path):
    docs_dir = tmp_path
    doc_dir = docs_dir / "General" / "DOC-9"
    doc_dir.mkdir(parents=True)
    dis.upsert_document(
        docs_dir,
        {"id": "DOC-9", "filename": "x", "content_type": "General", "source": "upload"},
    )
    write_search_cache(
        doc_dir,
        full_text="The payment terms are net thirty days.",
        sections=[],
        doc_id="DOC-9",
        docs_dir=docs_dir,
    )
    ids = dis.search_fts(docs_dir, "payment terms")
    assert "DOC-9" in ids
    assert dis.search_fts(docs_dir, "nonexistent-token-zzz") == []


_WRITE_ACTIONS = (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)


def _deny_one_commit_after_touching(conn, table: str) -> dict:
    """Make the next COMMIT that carries a write to ``table`` fail, once.

    A real SQLite authorizer, so the connection is left in the state a genuine
    commit failure leaves it in — the transaction still open — rather than a
    stand-in for it.
    """
    state = {"touched": False, "denied": False}

    def authorizer(action, arg1, arg2, dbname, source):
        if action in _WRITE_ACTIONS and arg1 == table:
            state["touched"] = True
        if (
            action == sqlite3.SQLITE_TRANSACTION
            and arg1 == "COMMIT"
            and state["touched"]
            and not state["denied"]
        ):
            state["denied"] = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    return state


def test_a_write_that_cannot_commit_is_not_committed_by_the_next_one(tmp_path: Path):
    """The failed update must not ride along on a later write's commit.

    Connections are cached per thread for the life of the process, and SQLite
    leaves a failed COMMIT's transaction open. The next write on that thread
    used to commit both — and the next write is often the rollback itself,
    which puts the old text back in the FTS table.
    """
    docs_dir = tmp_path
    entry = {
        "id": "DOC-1",
        "filename": "old.txt",
        "content_type": "General",
        "source": "upload",
    }
    dis.upsert_document(docs_dir, entry)

    conn = dis._connect(docs_dir)
    state = _deny_one_commit_after_touching(conn, "documents")
    with pytest.raises(sqlite3.DatabaseError):
        dis.upsert_document(docs_dir, {**entry, "filename": "new.txt"})
    assert state["denied"]
    assert not conn.in_transaction, "the failed write was left open to be committed later"

    # What a rollback does next: put the indexed text back. Same connection.
    dis.upsert_fts(docs_dir, "DOC-1", "the text that was there before")

    independent = sqlite3.connect(str(docs_dir / ".doc-index.sqlite"))
    try:
        row = independent.execute(
            "SELECT entry_json FROM documents WHERE id = 'DOC-1'"
        ).fetchone()
    finally:
        independent.close()
    assert json.loads(row[0])["filename"] == "old.txt"


def test_a_failed_delete_does_not_take_a_later_write_with_it(tmp_path: Path):
    """Same guarantee from the other side: the delete stays undone."""
    docs_dir = tmp_path
    dis.upsert_document(
        docs_dir,
        {"id": "DOC-2", "filename": "keep.txt", "content_type": "General", "source": "upload"},
    )
    conn = dis._connect(docs_dir)
    _deny_one_commit_after_touching(conn, "documents")
    with pytest.raises(sqlite3.DatabaseError):
        dis.delete_document(docs_dir, "DOC-2")

    dis.upsert_fts(docs_dir, "DOC-2", "still here")

    independent = sqlite3.connect(str(docs_dir / ".doc-index.sqlite"))
    try:
        count = independent.execute(
            "SELECT count(*) FROM documents WHERE id = 'DOC-2'"
        ).fetchone()[0]
    finally:
        independent.close()
    assert count == 1

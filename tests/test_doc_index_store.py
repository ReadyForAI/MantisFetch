"""B3: SQLite document index + FTS."""

from pathlib import Path

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

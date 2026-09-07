"""Searching a library reads its index once, not once per document.

`_resolve_doc_dir` looked its argument up by loading the whole index and
scanning it in Python, and the search loop called it for every document it
walked. A 1,000-document library therefore decoded 1,001,000 index rows to
answer one query — 2.7 seconds, quadratic in the library size, while the table
had `id` as its primary key the entire time.

The entry is passed in as a hint now. It stays a hint: a row whose
`storage_path` no longer holds a manifest still falls through to the layout
scan, so a stale index costs a scan and not a wrong answer.
"""

import json

import pytest


@pytest.fixture()
def docs(monkeypatch, tmp_path):
    import mantisfetch_common.storage as cs

    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


def _add(docs_dir, i, content_type="General", body="needle body"):
    from mantisfetch_docreader.storage import _update_doc_index

    doc_id = f"DOC-{i:05d}"
    doc_dir = docs_dir / content_type / doc_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "manifest.json").write_text(json.dumps({"doc_id": doc_id, "sections": []}))
    (doc_dir / "full.md").write_text(f"{body} {i}\n")
    _update_doc_index(
        docs_dir,
        {
            "doc_id": doc_id,
            "filename": f"f{i}.txt",
            "file_type": "txt",
            "total_pages": 1,
            "section_count": 1,
            "created_at": "2026-09-07T00:00:00Z",
            "storage_path": f"{content_type}/{doc_id}",
        },
        f"digest {i}",
        content_type=content_type,
    )
    return doc_dir


def _count_index_loads(monkeypatch):
    import mantisfetch_common.doc_index_store as dis

    calls = {"n": 0}
    real = dis.list_documents
    monkeypatch.setattr(
        dis, "list_documents", lambda d: (calls.__setitem__("n", calls["n"] + 1), real(d))[1]
    )
    return calls


def test_a_search_loads_the_index_once(docs, monkeypatch, client) -> None:
    for i in range(8):
        _add(docs, i)

    calls = _count_index_loads(monkeypatch)
    resp = client.get("/doc/library/search_text", params={"q": "needle", "scope": "full"})

    assert resp.status_code == 200
    assert resp.json()["total"] == 8
    assert calls["n"] == 1, f"{calls['n']} index loads for an 8-document library"


def test_reading_one_document_does_not_load_the_whole_index(docs, monkeypatch, client) -> None:
    """Every by-doc_id read went through the same scan: manifest, digest,
    sections, delete."""
    for i in range(8):
        _add(docs, i)

    calls = _count_index_loads(monkeypatch)
    client.get("/doc/library/DOC-00003/manifest")

    assert calls["n"] == 0, "a primary-key read still loaded every row"


def test_a_stale_index_row_still_finds_the_document(docs, client) -> None:
    """The entry is a hint, not an authority. A document whose directory moved
    without the index being updated has to be found by the layout scan, exactly
    as it was before the entry was passed in."""
    import shutil

    doc_dir = _add(docs, 42, content_type="General")
    moved = docs / "Contract" / "DOC-00042"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(doc_dir), str(moved))  # index still says General/

    resp = client.get("/doc/library/DOC-00042/manifest")
    assert resp.status_code == 200, "the layout-scan fallback stopped running"


def test_a_search_still_finds_a_document_the_index_points_at_wrongly(docs, client) -> None:
    import shutil

    _add(docs, 1)
    doc_dir = _add(docs, 2)
    moved = docs / "Bid" / "DOC-00002"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(doc_dir), str(moved))

    hits = client.get("/doc/library/search_text", params={"q": "needle", "scope": "full"}).json()
    assert {r["doc_id"] for r in hits["results"]} == {"DOC-00001", "DOC-00002"}


def test_a_legacy_json_only_library_is_migrated_by_a_point_read(docs) -> None:
    """`get_document` is the first thing a fresh process may call. Without the
    one-shot migration it would answer None for every document in a library
    that predates the database."""
    import mantisfetch_common.doc_index_store as dis

    (docs / "doc-index.json").write_text(
        json.dumps({"version": 2, "documents": [{"id": "DOC-00099", "filename": "legacy.txt"}]})
    )

    assert dis.get_document(docs, "DOC-00099") is not None


def test_a_missing_document_is_still_a_miss(docs) -> None:
    import mantisfetch_common.doc_index_store as dis

    _add(docs, 1)
    assert dis.get_document(docs, "DOC-99999") is None

"""B2: parse-time lowercase search cache for /library/search_text."""

from pathlib import Path

from mantisfetch_common.search_cache import (
    read_full_lower,
    read_sections_lower,
    write_search_cache,
)


def test_write_and_read_search_cache(tmp_path: Path):
    doc_dir = tmp_path / "General" / "DOC-1"
    doc_dir.mkdir(parents=True)
    write_search_cache(
        doc_dir,
        full_text="# Hello World\n\nPayment TERMS apply.",
        sections=[
            {
                "sid": "s_001",
                "title": "Clause A",
                "text": "Payment terms are net 30.",
                "file": "sections/01-s_001-Clause-A.md",
                "page_range": "1",
            }
        ],
    )
    full_l = read_full_lower(doc_dir)
    assert full_l is not None
    assert "payment terms apply" in full_l
    secs = read_sections_lower(doc_dir)
    assert secs is not None and len(secs) == 1
    assert secs[0]["title_lower"] == "clause a"
    assert "net 30" in secs[0]["text_lower"]


def test_search_text_uses_cache(client, tmp_path, monkeypatch):
    import mantisfetch_docreader as dr

    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(dr, "_get_docs_dir", lambda: docs)
    # Minimal doc on disk
    doc_id = "DOC-100"
    doc_dir = docs / "General" / doc_id
    sections = doc_dir / "sections"
    sections.mkdir(parents=True)
    full = "# Report\n\nUniqueTokenXYZ in the body.\n"
    (doc_dir / "full.md").write_text(full, encoding="utf-8")
    sec_body = "# Intro\n\nUniqueTokenXYZ section body.\n"
    (sections / "01-s_001-Intro.md").write_text(sec_body, encoding="utf-8")
    (doc_dir / "manifest.json").write_text(
        __import__("json").dumps(
            {
                "doc_id": doc_id,
                "sections": [
                    {
                        "sid": "s_001",
                        "title": "Intro",
                        "file": "sections/01-s_001-Intro.md",
                        "page_range": "1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_search_cache(
        doc_dir,
        full_text=full,
        sections=[
            {
                "sid": "s_001",
                "title": "Intro",
                "text": "UniqueTokenXYZ section body.",
                "file": "sections/01-s_001-Intro.md",
                "page_range": "1",
            }
        ],
    )
    (docs / "doc-index.json").write_text(
        __import__("json").dumps(
            {
                "version": 2,
                "documents": [
                    {
                        "id": doc_id,
                        "filename": "r.pdf",
                        "file_type": "pdf",
                        "content_type": "General",
                        "storage_path": f"General/{doc_id}",
                        "source": "upload",
                        "digest": "d",
                        "tags": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    resp = client.get("/doc/library/search_text", params={"q": "UniqueTokenXYZ"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(r["doc_id"] == doc_id for r in data["results"])

"""API-contract regression: search total/limit, section sid exactness, parse 422."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient


def _seed_doc(docs_dir: Path, doc_id: str, filename: str) -> Path:
    d = docs_dir / doc_id
    (d / "sections").mkdir(parents=True)
    (d / "sections" / "01-abc123-Intro.md").write_text("# Intro\n\nbody alpha", encoding="utf-8")
    (d / "sections" / "02-def456-Methods.md").write_text("# Methods\n\nbody beta", encoding="utf-8")
    (d / "digest.md").write_text("digest", encoding="utf-8")
    manifest = {
        "doc_id": doc_id, "filename": filename, "file_type": "pdf",
        "content_type": "General", "storage_path": doc_id,
        "sections": [
            {"sid": "abc123", "index": 1, "title": "Intro", "page_range": "p.1",
             "file": "sections/01-abc123-Intro.md"},
            {"sid": "def456", "index": 2, "title": "Methods", "page_range": "p.2",
             "file": "sections/02-def456-Methods.md"},
        ],
    }
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


def _seed_index(docs_dir: Path, docs: list[tuple[str, str]]) -> None:
    index = {
        "version": 2,
        "documents": [
            {"id": i, "filename": f, "file_type": "pdf", "content_type": "General",
             "storage_path": i, "digest": "report quarterly", "tags": [],
             "created_at": "2026-01-01T00:00:00Z"}
            for i, f in docs
        ],
    }
    (docs_dir / "doc-index.json").write_text(json.dumps(index), encoding="utf-8")


def test_search_total_is_true_match_count(client: TestClient):  # #28
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        docs = [("DOC-001", "report a.pdf"), ("DOC-002", "report b.pdf"), ("DOC-003", "report c.pdf")]
        for i, f in docs:
            _seed_doc(docs_dir, i, f)
        _seed_index(docs_dir, docs)
        with patch("larkscout_docreader._get_docs_dir", return_value=docs_dir):
            resp = client.get("/doc/library/search", params={"q": "report", "limit": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 2  # page size
        assert data["total"] == 3  # true match count


def test_search_negative_limit_is_clamped(client: TestClient):  # #40
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        docs = [("DOC-001", "report a.pdf"), ("DOC-002", "report b.pdf")]
        for i, f in docs:
            _seed_doc(docs_dir, i, f)
        _seed_index(docs_dir, docs)
        with patch("larkscout_docreader._get_docs_dir", return_value=docs_dir):
            resp = client.get("/doc/library/search", params={"q": "report", "limit": -1})
        assert resp.status_code == 200
        assert len(resp.json()["results"]) >= 1  # negative used to drop results


def test_get_section_matches_sid_exactly(client: TestClient):  # #39
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        _seed_doc(docs_dir, "DOC-001", "x.pdf")
        _seed_index(docs_dir, [("DOC-001", "x.pdf")])
        with patch("larkscout_docreader._get_docs_dir", return_value=docs_dir):
            ok = client.get("/doc/library/DOC-001/section/abc123")
            by_index = client.get("/doc/library/DOC-001/section/01")  # filename substring, not a sid
            by_title = client.get("/doc/library/DOC-001/section/Intro")
        assert ok.status_code == 200
        assert "Intro" in ok.json()["content"]
        assert by_index.status_code == 404  # old substring scan wrongly matched this
        assert by_title.status_code == 404


def test_parse_page_range_raises_422():  # #26 (ocr_pages / skip_ocr_pages)
    from fastapi import HTTPException
    from larkscout_docreader.pdf_planning import _parse_page_range

    with pytest.raises(HTTPException) as e1:
        _parse_page_range("not-a-range", 10)
    assert e1.value.status_code == 422


def test_parse_rejects_bad_client_parse_mode_with_422(client: TestClient):  # #26 (parse_mode)
    import io

    resp = client.post(
        "/doc/parse",
        files={"file": ("x.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"parse_mode": "turbo", "generate_summary": "false"},
    )
    assert resp.status_code == 422

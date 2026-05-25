"""Tests for TASK-022: document library endpoints, rate limiting, input validation."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_doc(docs_dir: Path, doc_id: str = "DOC-001") -> Path:
    """Create a minimal document directory with all tier files."""
    doc_dir = docs_dir / doc_id
    doc_dir.mkdir(parents=True)
    (doc_dir / "digest.md").write_text(f"# {doc_id} digest\n\nShort summary.", encoding="utf-8")
    (doc_dir / "brief.md").write_text(f"# {doc_id} brief\n\nDetailed brief.", encoding="utf-8")
    (doc_dir / "full.md").write_text(
        f"# {doc_id} full\n\nPayment terms require invoice submission within 30 days.",
        encoding="utf-8",
    )

    sections_dir = doc_dir / "sections"
    sections_dir.mkdir()
    (sections_dir / "01-abc123-Introduction.md").write_text(
        "# Introduction\n\nCustomer ACME requests payment terms within 30 days.", encoding="utf-8"
    )
    (sections_dir / "02-def456-Methods.md").write_text(
        "# Methods\n\nConfidentiality survives termination.", encoding="utf-8"
    )

    tables_dir = doc_dir / "tables"
    tables_dir.mkdir()
    (tables_dir / "table-01.md").write_text(
        "# Table 1\n\n| A | B |\n|---|---|\n| 1 | 2 |", encoding="utf-8"
    )

    manifest = {
        "doc_id": doc_id,
        "filename": "test.pdf",
        "file_type": "pdf",
        "source": "upload",
        "metadata": {"customer": "ACME", "contract_type": "MSA"},
        "source_file": {
            "kind": "upload",
            "filename": "test.pdf",
            "ref": "source/test.pdf",
            "sha256": "abc123",
            "size_bytes": 2048,
        },
        "paths": {
            "digest": "digest.md",
            "brief": "brief.md",
            "full": "full.md",
            "sections_dir": "sections/",
        },
        "sections": [
            {
                "sid": "abc123",
                "index": 1,
                "title": "Introduction",
                "page_range": "p.1",
                "page_start": 1,
                "page_end": 1,
                "char_count": 6,
                "type": "text",
                "summary_preview": "Hello.",
                "file": "sections/01-abc123-Introduction.md",
            },
            {
                "sid": "def456",
                "index": 2,
                "title": "Methods",
                "page_range": "p.2",
                "page_start": 2,
                "page_end": 2,
                "char_count": 6,
                "type": "text",
                "summary_preview": "World.",
                "file": "sections/02-def456-Methods.md",
            },
        ],
        "provenance": {
            "source": "upload",
            "source_kind": "upload",
            "source_filename": "test.pdf",
            "source_ref": "source/test.pdf",
            "source_sha256": "abc123",
            "source_size_bytes": 2048,
            "source_url": "test.pdf",
            "created_at": "2026-01-01T00:00:00Z",
            "content_hash": "sha256:abc",
        },
    }
    (doc_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Write doc-index.json
    index = {
        "version": 2,
        "documents": [
            {
                "id": doc_id,
                "filename": "test.pdf",
                "file_type": "pdf",
                "source": "upload",
                "source_url": "test.pdf",
                "pages": 2,
                "sections": 2,
                "ocr_pages": 0,
                "tables": 1,
                "digest": "Short summary.",
                "digest_path": f"docs/{doc_id}/digest.md",
                "tags": ["test", "Q3"],
                "created_at": "2026-01-01T00:00:00Z",
                "content_hash": "sha256:abc",
                "metadata": {"customer": "ACME", "contract_type": "MSA"},
                "source_ref": "source/test.pdf",
                "source_filename": "test.pdf",
                "source_sha256": "abc123",
                "source_available": True,
            }
        ],
    }
    (docs_dir / "doc-index.json").write_text(json.dumps(index), encoding="utf-8")
    source_dir = doc_dir / "source"
    source_dir.mkdir()
    (source_dir / "test.pdf").write_bytes(b"%PDF-1.4 fixture")
    return doc_dir


# ---------------------------------------------------------------------------
# Library tier endpoints: digest, brief, full
# ---------------------------------------------------------------------------


class TestLibraryDigest:
    def test_digest_returns_content(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/digest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert "digest" in data["content"].lower()

    def test_digest_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/digest")
        assert resp.status_code == 404


class TestLibraryBrief:
    def test_brief_returns_content(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/brief")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert "brief" in data["content"].lower()

    def test_brief_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/brief")
        assert resp.status_code == 404


class TestLibraryFull:
    def test_full_returns_content(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/full")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert "full" in data["content"].lower()

    def test_full_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/full")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Section & table endpoints
# ---------------------------------------------------------------------------


class TestLibrarySections:
    def test_list_sections(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/sections")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sections"]) == 2
        sids = [s["sid"] for s in data["sections"]]
        assert "abc123" in sids
        assert "def456" in sids

    def test_read_section_by_sid(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/section/abc123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sid"] == "abc123"
        assert "Introduction" in data["content"]

    def test_section_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/section/nonexistent")
        assert resp.status_code == 404

    def test_read_table(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/table/01")
        assert resp.status_code == 200
        assert "Table 1" in resp.json()["content"]

    def test_table_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/table/99")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------


class TestLibrarySearch:
    def test_search_by_keyword(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?q=test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["results"][0]["doc_id"] == "DOC-001"

    def test_search_by_tag(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?tags=Q3")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_search_by_file_type(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?file_type=pdf")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_search_no_match(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?q=zzz_no_match")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_search_by_metadata_filter(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?metadata.customer=ACME")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["results"][0]["metadata"]["customer"] == "ACME"

    def test_search_empty_index(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?q=anything")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ---------------------------------------------------------------------------
# Manifest endpoint
# ---------------------------------------------------------------------------


class TestLibraryManifest:
    def test_manifest_returns_json(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/manifest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert "provenance" in data
        assert "sections" in data
        assert data["metadata"]["customer"] == "ACME"
        assert data["source_file"]["ref"] == "source/test.pdf"

    def test_manifest_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/manifest")
        assert resp.status_code == 404


class TestLibrarySidecars:
    def test_sidecar_discovery_is_low_token_and_explicit(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            doc_dir = _setup_doc(Path(tmp))
            sidecar = {
                "version": 1,
                "doc_id": "DOC-001",
                "coordinate_system": "image_pixels",
                "pages": [
                    {
                        "page": 1,
                        "width": 400,
                        "height": 200,
                        "blocks": [
                            {
                                "block_id": "p1-b0001",
                                "text": "甲方",
                                "bbox": [80, 40, 160, 80],
                                "confidence": 0.9,
                            }
                        ],
                    }
                ],
            }
            (doc_dir / "ocr_blocks.json").write_text(json.dumps(sidecar), encoding="utf-8")
            tables = [
                {
                    "table_id": "table-01",
                    "page": 1,
                    "row_count": 2,
                    "column_count": 2,
                    "source": "layout",
                    "file": "tables/table-01.md",
                    "json_file": "tables/table-01.json",
                    "bbox": [80, 40, 240, 140],
                }
            ]
            (doc_dir / "tables.json").write_text(json.dumps(tables), encoding="utf-8")
            (doc_dir / "tables" / "table-01.json").write_text(
                json.dumps({"table_id": "table-01", "rows": []}), encoding="utf-8"
            )

            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                discovery = client.get("/doc/library/DOC-001/sidecars")
                pages = client.get("/doc/library/DOC-001/layout/pages")
                page = client.get("/doc/library/DOC-001/layout/page/1")
                table_json = client.get("/doc/library/DOC-001/table/01/json")
                table_md = client.get("/doc/library/DOC-001/table/01")
                manifest = client.get("/doc/library/DOC-001/manifest")

        assert discovery.status_code == 200
        body = discovery.json()
        assert body["layout"]["available"] is True
        assert body["layout"]["page_count"] == 1
        assert body["layout"]["block_count"] == 1
        discovery_payload = json.dumps(body, ensure_ascii=False)
        assert "p1-b0001" not in discovery_payload
        assert "甲方" not in discovery_payload
        assert body["tables"]["items"][0]["json_file"] == "tables/table-01.json"

        assert pages.status_code == 200
        assert pages.json()["pages"] == [{"page": 1, "width": 400, "height": 200, "block_count": 1}]
        assert page.status_code == 200
        assert page.json()["page"]["blocks"][0]["text"] == "甲方"
        assert table_json.status_code == 200
        assert table_json.json()["table"]["table_id"] == "table-01"
        assert table_md.status_code == 200
        assert "| A | B |" in table_md.json()["content"]
        assert manifest.status_code == 200
        assert "sidecars" not in manifest.json()

    def test_table_json_requires_structured_sidecar(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/table/01/json")

        assert resp.status_code == 404


class TestLibrarySearchText:
    def test_search_text_returns_section_match_and_page_hint(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search_text?q=payment&scope=section")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        first = data["results"][0]
        assert first["doc_id"] == "DOC-001"
        assert first["sid"] == "abc123"
        assert first["page_start"] == 1
        assert "payment" in first["snippet"].lower()

    def test_search_text_doc_id_falls_back_without_index(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            _setup_doc(docs_dir)
            (docs_dir / "doc-index.json").unlink()
            with patch("larkscout_docreader._get_docs_dir", return_value=docs_dir):
                resp = client.get(
                    "/doc/library/search_text?q=payment&scope=section&doc_id=DOC-001"
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["results"][0]["doc_id"] == "DOC-001"

    def test_search_text_ignores_section_paths_outside_sections(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            doc_dir = _setup_doc(docs_dir)
            (docs_dir / "leak.md").write_text(
                "secret needle outside doc sections",
                encoding="utf-8",
            )
            manifest_path = doc_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["sections"][0]["file"] = "../leak.md"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with patch("larkscout_docreader._get_docs_dir", return_value=docs_dir):
                resp = client.get("/doc/library/search_text?q=needle&scope=section")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_search_text_ignores_invalid_index_doc_ids(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            docs_dir = base_dir / "docs"
            docs_dir.mkdir()
            outside_dir = base_dir / "outside"
            outside_dir.mkdir()
            (outside_dir / "full.md").write_text("outside needle", encoding="utf-8")
            (outside_dir / "manifest.json").write_text(
                json.dumps({"doc_id": "../outside", "sections": []}),
                encoding="utf-8",
            )
            (docs_dir / "doc-index.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "documents": [
                            {
                                "id": "../outside",
                                "filename": "outside.pdf",
                                "file_type": "pdf",
                                "digest": "",
                                "tags": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("larkscout_docreader._get_docs_dir", return_value=docs_dir):
                resp = client.get("/doc/library/search_text?q=needle&scope=full")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestLibrarySectionSearchAndChunks:
    def test_search_sections_returns_section_provenance(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.post(
                    "/doc/library/DOC-001/search_sections",
                    json={"q": "payment", "include_content": True},
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        first = data["results"][0]
        assert first["sid"] == "abc123"
        assert first["page_start"] == 1
        assert "payment" in first["snippet"].lower()
        assert "Customer ACME" in first["content"]

    def test_chunk_document_uses_section_boundaries(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.post(
                    "/doc/library/DOC-001/chunks",
                    json={"max_tokens_per_chunk": 4000, "include_text": False},
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert data["chunk_count"] >= 1
        first = data["chunks"][0]
        assert first["chunk_id"] == "chunk-0001"
        assert first["section_ids"]
        assert first["provenance"][0]["doc_id"] == "DOC-001"
        assert "text" not in first


# ---------------------------------------------------------------------------
# Rate limiting (429)
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_parse_sem_locked_returns_429(self, client: TestClient):
        """When _parse_sem is fully acquired, parse should return 429."""
        import larkscout_docreader

        original_sem = larkscout_docreader._parse_sem
        # Replace with a semaphore of size 0 (always locked)
        import asyncio

        locked_sem = asyncio.Semaphore(0)
        larkscout_docreader._parse_sem = locked_sem
        try:
            resp = client.post(
                "/doc/parse",
                files={"file": ("test.pdf", b"%PDF-1.4 minimal", "application/pdf")},
            )
            assert resp.status_code == 429
        finally:
            larkscout_docreader._parse_sem = original_sem

    def test_capture_sem_locked_returns_429(self, client: TestClient):
        """When _capture_sem is fully acquired, capture should return 429."""
        import larkscout_browser

        original_sem = larkscout_browser._capture_sem
        import asyncio

        locked_sem = asyncio.Semaphore(0)
        larkscout_browser._capture_sem = locked_sem
        try:
            resp = client.post(
                "/web/capture",
                json={"url": "https://example.com"},
            )
            assert resp.status_code == 429
        finally:
            larkscout_browser._capture_sem = original_sem

    def test_session_sem_locked_returns_429(self, client: TestClient):
        """When _session_sem is fully acquired, new session should return 429."""
        import larkscout_browser

        original_sem = larkscout_browser._session_sem
        import asyncio

        locked_sem = asyncio.Semaphore(0)
        larkscout_browser._session_sem = locked_sem
        try:
            resp = client.post("/web/session/new", json={})
            assert resp.status_code == 429
        finally:
            larkscout_browser._session_sem = original_sem


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_parse_unsupported_format(self, client: TestClient):
        resp = client.post(
            "/doc/parse",
            files={"file": ("test.bin", b"\x00\x01\x02", "application/octet-stream")},
        )
        assert resp.status_code == 422

    def test_parse_no_file(self, client: TestClient):
        resp = client.post("/doc/parse")
        assert resp.status_code == 422

    def test_doc_id_traversal_blocked(self, client: TestClient):
        resp = client.get("/doc/library/../etc/passwd/digest")
        assert resp.status_code in (400, 404, 422)

    def test_table_id_traversal_blocked(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/table/../../etc/passwd")
        assert resp.status_code in (400, 404, 422)

    def test_capture_invalid_url_blocked(self, client: TestClient):
        resp = client.post("/web/capture", json={"url": "file:///etc/passwd"})
        assert resp.status_code in (400, 422)

    def test_capture_private_ip_blocked(self, client: TestClient):
        resp = client.post("/web/capture", json={"url": "http://169.254.169.254/latest"})
        assert resp.status_code in (400, 422)

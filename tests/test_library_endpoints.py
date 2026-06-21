"""Tests for TASK-022: document library endpoints, rate limiting, input validation."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_doc(docs_dir: Path, doc_id: str = "DOC-001", content_type: str | None = None) -> Path:
    """Create a minimal document directory with all tier files."""
    storage_path = f"{content_type}/{doc_id}" if content_type else doc_id
    doc_dir = docs_dir / storage_path
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
        "content_type": content_type or "General",
        "storage_path": storage_path,
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
                "content_type": content_type or "General",
                "storage_path": storage_path,
                "source": "upload",
                "source_url": "test.pdf",
                "pages": 2,
                "sections": 2,
                "ocr_pages": 0,
                "tables": 1,
                "digest": "Short summary.",
                "digest_path": f"docs/{storage_path}/digest.md",
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
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/digest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert "digest" in data["content"].lower()

    def test_digest_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/digest")
        assert resp.status_code == 404


class TestLibraryBrief:
    def test_brief_returns_content(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/brief")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert "brief" in data["content"].lower()

    def test_brief_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/brief")
        assert resp.status_code == 404


class TestLibraryFull:
    def test_full_returns_content(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/full")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert "full" in data["content"].lower()

    def test_full_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/full")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Section & table endpoints
# ---------------------------------------------------------------------------


class TestLibrarySections:
    def test_list_sections(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
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
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/section/abc123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sid"] == "abc123"
        assert "Introduction" in data["content"]

    def test_section_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/section/nonexistent")
        assert resp.status_code == 404

    def test_sections_batch_returns_requested_and_missing(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.post(
                    "/doc/library/DOC-001/sections/batch",
                    json={"sids": ["abc123", "def456", "nonexistent", "abc123"]},
                )
        assert resp.status_code == 200
        data = resp.json()
        # found in request order, deduped (abc123 once), missing reported
        assert [s["sid"] for s in data["sections"]] == ["abc123", "def456"]
        assert "Introduction" in data["sections"][0]["content"]
        assert data["missing"] == ["nonexistent"]

    def test_sections_batch_requires_non_empty(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.post("/doc/library/DOC-001/sections/batch", json={"sids": []})
        assert resp.status_code == 422

    def test_sections_batch_doc_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.post(
                    "/doc/library/DOC-999/sections/batch", json={"sids": ["abc123"]}
                )
        assert resp.status_code == 404

    def test_read_table(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/table/01")
        assert resp.status_code == 200
        assert "Table 1" in resp.json()["content"]

    def test_table_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/table/99")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------


class TestLibrarySearch:
    def test_search_by_keyword(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?q=test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["results"][0]["doc_id"] == "DOC-001"

    def test_search_by_tag(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?tags=Q3")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_search_by_file_type(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?file_type=pdf")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_search_no_match(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?q=zzz_no_match")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_search_by_metadata_filter(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?metadata.customer=ACME")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["results"][0]["metadata"]["customer"] == "ACME"

    def test_search_empty_index(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
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
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
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
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/manifest")
        assert resp.status_code == 404

    def test_categorized_doc_is_read_by_doc_id(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            _setup_doc(docs_dir, doc_id="DOC-777", content_type="Contract")
            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir):
                manifest = client.get("/doc/library/DOC-777/manifest")
                digest = client.get("/doc/library/DOC-777/digest")
                search = client.get("/doc/library/search?content_type=Contract")
                text_search = client.get(
                    "/doc/library/search_text?q=payment&scope=section&content_type=Contract"
                )

        assert manifest.status_code == 200
        assert manifest.json()["content_type"] == "Contract"
        assert digest.status_code == 200
        assert "digest" in digest.json()["content"].lower()
        assert search.status_code == 200
        assert search.json()["results"][0]["doc_id"] == "DOC-777"
        assert search.json()["results"][0]["content_type"] == "Contract"
        assert text_search.status_code == 200
        assert text_search.json()["results"][0]["doc_id"] == "DOC-777"


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

            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
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
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/table/01/json")

        assert resp.status_code == 404


class TestLibrarySearchText:
    def test_search_text_returns_section_match_and_page_hint(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
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
            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir):
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

            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir):
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

            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir):
                resp = client.get("/doc/library/search_text?q=needle&scope=full")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestLibrarySectionSearchAndChunks:
    def test_search_sections_returns_section_provenance(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
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
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
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
        import mantisfetch_docreader

        original_sem = mantisfetch_docreader._parse_sem
        # Replace with a semaphore of size 0 (always locked)
        import asyncio

        locked_sem = asyncio.Semaphore(0)
        mantisfetch_docreader._parse_sem = locked_sem
        try:
            resp = client.post(
                "/doc/parse",
                files={"file": ("test.pdf", b"%PDF-1.4 minimal", "application/pdf")},
            )
            assert resp.status_code == 429
        finally:
            mantisfetch_docreader._parse_sem = original_sem

    def test_capture_sem_locked_returns_429(self, client: TestClient):
        """When _capture_sem is fully acquired, capture should return 429."""
        import mantisfetch_browser

        original_sem = mantisfetch_browser._capture_sem
        import asyncio

        locked_sem = asyncio.Semaphore(0)
        mantisfetch_browser._capture_sem = locked_sem
        try:
            resp = client.post(
                "/web/capture",
                json={"url": "https://example.com"},
            )
            assert resp.status_code == 429
        finally:
            mantisfetch_browser._capture_sem = original_sem

    def test_session_sem_locked_returns_429(self, client: TestClient):
        """When _session_sem is fully acquired, new session should return 429."""
        import mantisfetch_browser

        original_sem = mantisfetch_browser._session_sem
        import asyncio

        locked_sem = asyncio.Semaphore(0)
        mantisfetch_browser._session_sem = locked_sem
        try:
            resp = client.post("/web/session/new", json={})
            assert resp.status_code == 429
        finally:
            mantisfetch_browser._session_sem = original_sem


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
            with patch("mantisfetch_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/table/../../etc/passwd")
        assert resp.status_code in (400, 404, 422)

    def test_capture_invalid_url_blocked(self, client: TestClient):
        resp = client.post("/web/capture", json={"url": "file:///etc/passwd"})
        assert resp.status_code in (400, 422)

    def test_capture_private_ip_blocked(self, client: TestClient):
        resp = client.post("/web/capture", json={"url": "http://169.254.169.254/latest"})
        assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# /doc/parse explicit doc_id collision protection (Layer 1 dedup)
# ---------------------------------------------------------------------------


class TestRetrySummaryPreservesExtracted:
    """C2: a summary-only rewrite must not delete extracted tables/images/ocr."""

    def _seed_doc(self, docs_dir: Path, doc_id: str = "DOC-901") -> Path:
        d = docs_dir / "General" / doc_id
        (d / "sections").mkdir(parents=True)
        (d / "tables").mkdir()
        (d / "images").mkdir()
        (d / "sections" / "01-s1-Intro.md").write_text("# Intro\n\nbody\n", encoding="utf-8")
        (d / "tables.json").write_text(json.dumps([{"table_id": "01", "page": 1}]), encoding="utf-8")
        (d / "tables" / "01.md").write_text("|a|b|\n|-|-|\n|1|2|\n", encoding="utf-8")
        (d / "images.json").write_text(json.dumps([{"image_id": "01"}]), encoding="utf-8")
        (d / "images" / "01.png").write_bytes(b"\x89PNG")
        (d / "ocr_blocks.json").write_text(json.dumps({"pages": []}), encoding="utf-8")
        manifest = {
            "doc_id": doc_id, "filename": "f.pdf", "file_type": "pdf",
            "total_pages": 1, "section_count": 1, "table_count": 1, "image_count": 1,
            "ocr_page_count": 0, "content_type": "General", "storage_path": f"General/{doc_id}",
            "parse_metadata": {"total_pages": 1},
            "sections": [{"index": 1, "sid": "s1", "title": "Intro", "page_range": "p.1",
                          "file": "sections/01-s1-Intro.md", "image_refs": []}],
            "tables": [{"table_id": "01", "page": 1}],
            "images": [{"image_id": "01"}],
            "layout": {"available": True, "ocr_blocks_path": "ocr_blocks.json"},
        }
        (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return d

    def test_summary_rewrite_keeps_tables_images_ocr(self, monkeypatch, tmp_path):
        import mantisfetch_docreader as dr

        import mantisfetch_common.storage as cs

        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", docs_dir)
        d = self._seed_doc(docs_dir)

        parsed, metadata, source_record = dr._load_parsed_document_from_storage(docs_dir, "DOC-901")
        assert parsed.table_count == 0 and not parsed.images  # reconstructed: empty extracted

        dr.write_output_extract_only(
            "DOC-901", parsed, docs_dir, content_type="General",
            metadata=metadata, source_record=source_record, preserve_extracted=True,
            summary_placeholder="pending",
        )

        assert (d / "tables.json").exists(), "tables.json was deleted"
        assert (d / "tables" / "01.md").exists(), "table file was deleted"
        assert (d / "images.json").exists(), "images.json was deleted"
        assert (d / "ocr_blocks.json").exists(), "ocr sidecar was deleted"
        m = json.loads((d / "manifest.json").read_text())
        assert m["table_count"] == 1 and m["image_count"] == 1
        assert m["tables"] and m["images"]

    def test_full_rewrite_without_preserve_resets_extracted(self, monkeypatch, tmp_path):
        # Control: the default (parse) path still regenerates from parsed.
        import mantisfetch_docreader as dr

        import mantisfetch_common.storage as cs

        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", docs_dir)
        d = self._seed_doc(docs_dir)
        parsed, metadata, source_record = dr._load_parsed_document_from_storage(docs_dir, "DOC-901")

        dr.write_output_extract_only(
            "DOC-901", parsed, docs_dir, content_type="General",
            metadata=metadata, source_record=source_record, preserve_extracted=False,
            summary_placeholder="pending",
        )
        # parsed has no tables/images → regeneration writes none → they're gone.
        assert not (d / "tables.json").exists()
        m = json.loads((d / "manifest.json").read_text())
        assert m["table_count"] == 0


class TestParseReplaceProtection:
    def test_collision_without_replace_returns_409(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            existing_id = "REPLACE-TEST-001"
            _setup_doc(docs_dir, existing_id, content_type="Contract")
            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir):
                resp = client.post(
                    "/doc/parse",
                    files={"file": ("test.pdf", b"%PDF-1.4 minimal", "application/pdf")},
                    data={"doc_id": existing_id},
                )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert existing_id in detail
        assert "replace=true" in detail

    def test_collision_with_replace_bypasses_check(self, client: TestClient):
        """replace=true must let the handler proceed past the 409 guard.

        We send a stub PDF body and only assert the collision check itself
        was not raised; downstream parse-level errors are out of scope here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            existing_id = "REPLACE-TEST-002"
            _setup_doc(docs_dir, existing_id, content_type="Contract")
            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir):
                resp = client.post(
                    "/doc/parse",
                    files={"file": ("test.pdf", b"%PDF-1.4 minimal", "application/pdf")},
                    data={"doc_id": existing_id, "replace": "true"},
                )
        assert resp.status_code != 409

    def test_parse_response_model_defaults_dedup_to_miss(self):
        import mantisfetch_docreader

        fields = mantisfetch_docreader.ParseResponse.model_fields
        assert "dedup" in fields
        assert fields["dedup"].default == "miss"

    def test_replace_preserves_existing_content_type(self, client: TestClient):
        """replace=true must reuse the existing doc's content_type so the
        new write lands in the same category directory instead of orphaning
        the old artifacts under Contract/ and writing fresh ones under General/."""
        sample_pdf = Path(__file__).parent / "e2e" / "fixtures" / "sample.pdf"
        if not sample_pdf.exists():
            pytest.skip(f"sample.pdf fixture missing: {sample_pdf}")
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            existing_id = "REPLACE-TEST-003"
            _setup_doc(docs_dir, existing_id, content_type="Contract")
            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir):
                resp = client.post(
                    "/doc/parse",
                    files={
                        "file": ("sample.pdf", sample_pdf.read_bytes(), "application/pdf"),
                    },
                    data={
                        "doc_id": existing_id,
                        "replace": "true",
                        "content_type": "General",
                        "generate_summary": "false",
                    },
                )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dedup"] == "replaced"
        assert body["content_type"] == "Contract"
        assert body["storage_path"] == f"Contract/{existing_id}"

    @pytest.mark.asyncio
    async def test_optional_doc_id_lock_reuses_lock_per_id(self):
        """While a request is inside `async with _optional_doc_id_lock(id)`,
        any concurrent same-id lookup must see the same Lock instance —
        that's how concurrent same-explicit-id parses serialize."""
        import mantisfetch_docreader

        async with mantisfetch_docreader._optional_doc_id_lock("LOCK-A"):
            lock_a = mantisfetch_docreader._doc_id_parse_locks["LOCK-A"]
            lock_a_again = mantisfetch_docreader._doc_id_parse_locks["LOCK-A"]
            assert lock_a is lock_a_again
            async with mantisfetch_docreader._optional_doc_id_lock("LOCK-B"):
                lock_b = mantisfetch_docreader._doc_id_parse_locks["LOCK-B"]
                assert lock_b is not lock_a

    @pytest.mark.asyncio
    async def test_optional_doc_id_lock_entry_collected_after_release(self):
        """WeakValueDictionary releases entries once no request still holds
        the lock — keeps long-running servers from leaking one Lock per id
        when callers churn through high-cardinality explicit doc_ids."""
        import gc

        import mantisfetch_docreader

        mantisfetch_docreader._doc_id_parse_locks.pop("LOCK-EPHEMERAL", None)
        async with mantisfetch_docreader._optional_doc_id_lock("LOCK-EPHEMERAL"):
            assert "LOCK-EPHEMERAL" in mantisfetch_docreader._doc_id_parse_locks
        gc.collect()
        assert "LOCK-EPHEMERAL" not in mantisfetch_docreader._doc_id_parse_locks

    @pytest.mark.asyncio
    async def test_retry_summary_serializes_on_doc_lock(self, monkeypatch, tmp_path):
        """C13/C47: retry_summary must hold the per-doc_id lock so it can't race
        a concurrent parse/retry — and it must use the (formerly dead) helper."""
        import asyncio

        import mantisfetch_docreader as dr
        from mantisfetch_docreader.models import ParsedDocument

        parsed = ParsedDocument(
            filename="f", file_type="pdf", total_pages=0, pages=[], sections=[], metadata={}
        )
        monkeypatch.setattr(dr, "_get_docs_dir", lambda: tmp_path)
        monkeypatch.setattr(dr, "_load_parsed_document_from_storage", lambda d, i: (parsed, {}, {}))
        monkeypatch.setattr(dr, "_load_doc_tags", lambda d, i: [])
        monkeypatch.setattr(dr, "_doc_content_type", lambda d, i: "General")
        monkeypatch.setattr(dr, "write_output_extract_only", lambda *a, **k: None)
        monkeypatch.setattr(dr, "_generate_deferred_summary", lambda *a, **k: None)

        dr._doc_id_parse_locks.pop("RETRY-1", None)
        # Hold the per-doc lock; retry_summary on the same id must block on it.
        async with dr._optional_doc_id_lock("RETRY-1"):
            task = asyncio.create_task(dr.retry_summary("RETRY-1"))
            await asyncio.sleep(0.05)
            assert not task.done(), "retry_summary did not wait on the per-doc lock"
        # Lock released → retry proceeds.
        result = await asyncio.wait_for(task, timeout=2)
        assert result["scheduled"] is True

    @pytest.mark.asyncio
    async def test_retry_summary_claims_running_then_rejects_concurrent(self, monkeypatch, tmp_path):
        """C13: a retry claims the slot by marking running; a second retry then
        409s (no duplicate worker), but force overrides."""
        import mantisfetch_docreader as dr
        from mantisfetch_docreader.models import ParsedDocument

        parsed = ParsedDocument(
            filename="f", file_type="pdf", total_pages=0, pages=[], sections=[], metadata={}
        )
        monkeypatch.setattr(dr, "_get_docs_dir", lambda: tmp_path)
        monkeypatch.setattr(dr, "_load_parsed_document_from_storage", lambda d, i: (parsed, {}, {}))
        monkeypatch.setattr(dr, "_load_doc_tags", lambda d, i: [])
        monkeypatch.setattr(dr, "_doc_content_type", lambda d, i: "General")
        monkeypatch.setattr(dr, "write_output_extract_only", lambda *a, **k: None)
        monkeypatch.setattr(dr, "_generate_deferred_summary", lambda *a, **k: None)
        dr._doc_id_parse_locks.pop("RETRY-2", None)

        first = await dr.retry_summary("RETRY-2")  # claims slot -> status=running
        assert first["scheduled"] is True
        with pytest.raises(HTTPException) as exc:  # worker stubbed → still running
            await dr.retry_summary("RETRY-2")
        assert exc.value.status_code == 409
        forced = await dr.retry_summary("RETRY-2", force=True)
        assert forced["scheduled"] is True

    @pytest.mark.asyncio
    async def test_retry_summary_allows_stale_pending(self, monkeypatch, tmp_path):
        """C13: a doc left 'pending' by the parse path (worker never ran) must
        still be retryable without force — pending is not treated as in-flight."""
        import mantisfetch_docreader as dr
        from mantisfetch_docreader.models import ParsedDocument

        parsed = ParsedDocument(
            filename="f", file_type="pdf", total_pages=0, pages=[], sections=[],
            metadata={"summary": {"status": "pending", "attempts": 0}},
        )
        monkeypatch.setattr(dr, "_get_docs_dir", lambda: tmp_path)
        monkeypatch.setattr(dr, "_load_parsed_document_from_storage", lambda d, i: (parsed, {}, {}))
        monkeypatch.setattr(dr, "_load_doc_tags", lambda d, i: [])
        monkeypatch.setattr(dr, "_doc_content_type", lambda d, i: "General")
        monkeypatch.setattr(dr, "write_output_extract_only", lambda *a, **k: None)
        monkeypatch.setattr(dr, "_generate_deferred_summary", lambda *a, **k: None)
        dr._doc_id_parse_locks.pop("RETRY-3", None)

        result = await dr.retry_summary("RETRY-3")  # pending → not rejected
        assert result["scheduled"] is True

    @pytest.mark.asyncio
    async def test_next_filename_doc_id_skips_reserved_id(self):
        """When a same-base id is reserved in _doc_id_parse_locks (an
        in-flight parse), the resolver must roll to the next candidate so two
        concurrent same-filename uploads don't both pick the same id."""
        import asyncio

        import mantisfetch_docreader

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            held_lock = asyncio.Lock()
            mantisfetch_docreader._doc_id_parse_locks["report1"] = held_lock
            try:
                picked = mantisfetch_docreader._next_filename_doc_id(docs_dir, "report1.pdf")
                assert picked == "report1-2"
            finally:
                mantisfetch_docreader._doc_id_parse_locks.pop("report1", None)
                del held_lock

    @pytest.mark.asyncio
    async def test_next_filename_doc_id_handles_max_length_base(self):
        """An 80-char sanitized base that's already reserved would otherwise
        loop forever because `f"{base}-2"[:80]` is just `base`. The resolver
        must reserve room for the suffix and produce a distinct candidate."""
        import asyncio

        import mantisfetch_docreader

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            # 80-char base ending in a digit so _DOC_ID_RE matches.
            base = ("a" * 79) + "1"
            assert len(base) == 80
            held_lock = asyncio.Lock()
            mantisfetch_docreader._doc_id_parse_locks[base] = held_lock
            try:
                picked = mantisfetch_docreader._next_filename_doc_id(docs_dir, f"{base}.pdf")
                assert picked != base
                assert picked is not None and picked.endswith("-2")
                assert len(picked) <= 80
            finally:
                mantisfetch_docreader._doc_id_parse_locks.pop(base, None)
                del held_lock

    def test_oversized_upload_does_not_advance_counter(self, client: TestClient):
        """413 on oversized upload must not burn a counter slot — the next
        well-sized upload should still get DOC-001."""
        import mantisfetch_docreader

        oversize = b"x" * (mantisfetch_docreader.MAX_UPLOAD_BYTES + 1)
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir):
                resp_big = client.post(
                    "/doc/parse",
                    files={"file": ("huge.pdf", oversize, "application/pdf")},
                )
                assert resp_big.status_code == 413
                # Counter file should not have been created/advanced
                counter_path = docs_dir / ".counter"
                assert not counter_path.exists()

    def test_oversized_upload_cleans_scratch_file(self, client: TestClient):
        """413 must remove the streamed scratch tempfile — repeated rejected
        uploads would otherwise accumulate large `mantisfetch-upload-*` files
        in the system tmp dir."""
        import mantisfetch_docreader

        before = {p.name for p in Path(tempfile.gettempdir()).glob("mantisfetch-upload-*")}
        oversize = b"x" * (mantisfetch_docreader.MAX_UPLOAD_BYTES + 1)
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir):
                resp = client.post(
                    "/doc/parse",
                    files={"file": ("huge.pdf", oversize, "application/pdf")},
                )
                assert resp.status_code == 413
        after = {p.name for p in Path(tempfile.gettempdir()).glob("mantisfetch-upload-*")}
        leaked = after - before
        assert leaked == set(), f"scratch files leaked after 413: {leaked}"

    @pytest.mark.parametrize(
        "field,value,expected_message",
        [
            ({"metadata": "{not valid json"}, None, "metadata"),
            ({"content_type": "BogusCategory"}, None, "content_type"),
            ({"image_ocr_backend": "nope"}, None, "image_ocr_backend"),
        ],
        ids=["bad_metadata_json", "bad_content_type", "bad_image_ocr_backend"],
    )
    def test_validation_failures_do_not_advance_counter(
        self, client: TestClient, field, value, expected_message
    ):
        """422 from form validation must not advance .counter — issue #58.

        With the default counter id_strategy, any path that calls
        _resolve_doc_id before validating cheap form fields will burn a
        DOC-NNN slot for requests that never succeed, slowly leaving gaps
        in the doc id sequence.
        """
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir):
                resp = client.post(
                    "/doc/parse",
                    files={"file": ("ok.pdf", b"%PDF-1.4 minimal", "application/pdf")},
                    data=field,
                )
                assert resp.status_code == 422, resp.text
                assert expected_message in resp.json()["detail"].lower()
                counter_path = docs_dir / ".counter"
                assert not counter_path.exists(), (
                    f".counter advanced on 422; gap left at {counter_path.read_text()}"
                )

    def test_docx_ocr_overlimit_does_not_advance_counter(self, client: TestClient):
        """422 from Word OCR image overlimit must not advance .counter —
        issue #67. The check is gated on docx + extract_images + ocr_images,
        so we build a docx with 3 embedded images and set max_ocr_images=2."""
        import io
        import zipfile

        from PIL import Image

        png = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(png, format="PNG")
        png_bytes = png.getvalue()
        buf = io.BytesIO()
        rels = "\n".join(
            f'<Relationship Id="rId{i}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            f'Target="media/image{i}.png"/>'
            for i in range(1, 4)
        )
        body = "\n".join(
            f'<w:p><w:r><w:drawing><a:blip r:embed="rId{i}"/></w:drawing></w:r></w:p>'
            for i in range(1, 4)
        )
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
            ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>"
        )
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{rels}</Relationships>"
        )
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/word/document.xml"'
            ' ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>"
        )
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", content_types)
            zf.writestr("word/document.xml", document_xml)
            zf.writestr("word/_rels/document.xml.rels", rels_xml)
            for i in range(1, 4):
                zf.writestr(f"word/media/image{i}.png", png_bytes)

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir):
                resp = client.post(
                    "/doc/parse",
                    files={"file": ("with-images.docx", buf.getvalue(), "application/octet-stream")},
                    data={
                        "extract_images": "true",
                        "ocr_images": "true",
                        "max_ocr_images": "2",
                    },
                )
                assert resp.status_code == 422, resp.text
                assert "image OCR refused" in resp.json()["detail"]
                counter_path = docs_dir / ".counter"
                assert not counter_path.exists(), (
                    f".counter advanced on 422; gap left at {counter_path.read_text()}"
                )

    def test_early_ocr_check_clamps_max_images_like_lock(self, client: TestClient):
        """Pre-resolve OCR check must mirror the in-lock 0..1000 clamp on
        max_images/max_ocr_images. Without it, a request like
        max_images=2000+embedded>1000 would 422 here even though the lock
        path would accept it after clamping. Flagged by Codex review."""
        import io
        import zipfile

        from PIL import Image

        png = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(png, format="PNG")
        png_bytes = png.getvalue()
        buf = io.BytesIO()
        # Just 3 real images — we mock the count to simulate >1000.
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
            )
            zf.writestr("word/media/image1.png", png_bytes)

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            with patch("mantisfetch_docreader._get_docs_dir", return_value=docs_dir), patch(
                "mantisfetch_docreader._count_word_embedded_image_references", return_value=1001
            ):
                resp = client.post(
                    "/doc/parse",
                    files={"file": ("big.docx", buf.getvalue(), "application/octet-stream")},
                    data={
                        "extract_images": "true",
                        "ocr_images": "true",
                        "max_images": "2000",
                        "max_ocr_images": "1000",
                    },
                )
                # With clamping: max_images -> 1000, requested = min(1001, 1000) = 1000,
                # max_ocr_images -> 1000, 1000 > 1000 is False → no 422 from this check.
                # Without clamping (bug): requested = min(1001, 2000) = 1001 > 1000 → 422.
                # We assert the bug is fixed by checking the response is NOT 422 from
                # this specific path; downstream parse errors are OK and out of scope.
                if resp.status_code == 422:
                    assert "image OCR refused" not in resp.json().get("detail", ""), (
                        "Early OCR check 422'd before clamping max_images — "
                        f"Codex P2 regression. Body: {resp.text}"
                    )

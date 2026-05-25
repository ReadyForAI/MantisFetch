"""Tests for POST /web/capture (one-shot web capture endpoint)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient


def _make_distill_result(url: str = "https://example.com") -> dict:
    """Return a minimal distill result that _persist_web_capture can consume."""
    return {
        "url": url,
        "title": "Example Domain",
        "content_hash": "sha256:abc123",
        "sections": [
            {"sid": "s_0001", "h": "Introduction", "t": "This is example content.", "type": "text"},
            {"sid": "s_0002", "h": None, "t": "More text here.", "type": "text"},
            {
                "sid": "s_0003",
                "h": "[Table] Data",
                "t": "| Col A | Col B |\n| --- | --- |\n| 1 | 2 |",
                "type": "table",
                "table_meta": {"rows": 2, "cols": 2, "has_header": True, "truncated": False},
            },
        ],
        "actions": [],
        "meta": {},
    }


def test_capture_empty_body_returns_422(client: TestClient) -> None:
    """POST /web/capture with empty body must return 422 (field validation), not 404."""
    resp = client.post("/web/capture", json={})
    assert resp.status_code == 422


def test_capture_persists_to_doc_library(client: TestClient) -> None:
    """POST /web/capture with a valid URL writes files and returns doc metadata."""
    distill_result = _make_distill_result()

    with tempfile.TemporaryDirectory() as tmp_dir:
        docs_dir = Path(tmp_dir)

        with (
            patch("larkscout_browser._get_docs_dir", return_value=docs_dir),
            patch("larkscout_browser._distill", new=AsyncMock(return_value=distill_result)),
            patch("larkscout_browser._browser", new=MagicMock()),
            patch("larkscout_browser._setup_routing", new=AsyncMock()),
        ):
            # Mock the browser context/page creation chain
            mock_page = AsyncMock()
            mock_page.goto = AsyncMock()
            mock_context = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)

            import larkscout_browser as lb
            orig_browser = lb._browser
            lb._browser = MagicMock()
            lb._browser.new_context = AsyncMock(return_value=mock_context)

            try:
                resp = client.post(
                    "/web/capture",
                    json={
                        "url": "https://example.com",
                        "content_type": "Knowledge",
                        "tags": ["test"],
                        "extract_tables": True,
                    },
                )
            finally:
                lb._browser = orig_browser

        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"].startswith("WEB-")
        assert isinstance(data["digest"], str) and len(data["digest"]) > 0
        assert data["section_count"] == 3
        assert data["table_count"] == 1

        # Files should be written to the temp docs dir
        doc_dir = docs_dir / "Knowledge" / data["doc_id"]
        assert (doc_dir / "digest.md").exists()
        assert (doc_dir / "manifest.json").exists()
        assert (doc_dir / "sections").is_dir()
        assert (doc_dir / "tables").is_dir()

        # doc-index.json should be updated
        index = json.loads((docs_dir / "doc-index.json").read_text())
        assert index["version"] == 2
        ids = [d["id"] for d in index["documents"]]
        assert data["doc_id"] in ids

        entry = next(d for d in index["documents"] if d["id"] == data["doc_id"])
        assert data["content_type"] == "Knowledge"
        assert entry["content_type"] == "Knowledge"
        assert entry["storage_path"] == f"Knowledge/{data['doc_id']}"
        assert entry["source"] == "web_capture"
        assert entry["tags"] == ["test"]
        assert entry["source_url"] == "https://example.com"


def test_capture_browser_not_ready(client: TestClient) -> None:
    """POST /web/capture returns 500 when the browser is not initialised."""
    import larkscout_browser as lb

    orig_browser = lb._browser
    lb._browser = None
    try:
        resp = client.post(
            "/web/capture",
            json={"url": "https://example.com"},
        )
    finally:
        lb._browser = orig_browser

    assert resp.status_code == 500

"""Tests for POST /web/capture (one-shot web capture endpoint)."""

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient


def _seed_capture_index(
    docs_dir: Path,
    *,
    doc_id: str = "WEB-001",
    url: str = "https://example.com",
    content_type: str = "General",
    age_hours: float = 1.0,
    digest: str = "cached digest",
    extract_tables: bool = True,
    requested_url: str | None = None,
) -> dict:
    """Write a doc-index.json with one web_capture entry created age_hours ago,
    plus its digest.md (index stores only the 200-char preview, like production).
    source_url is the (possibly post-redirect) final URL; requested_url is the
    caller-supplied one used as the dedup key (defaults to url)."""
    created = (datetime.now(UTC) - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    storage_path = f"{content_type}/{doc_id}"
    entry = {
        "id": doc_id, "filename": "Example", "file_type": "web_capture",
        "content_type": content_type, "storage_path": storage_path,
        "source": "web_capture", "source_url": url, "pages": 1,
        "sections": 3, "ocr_pages": 0, "tables": 1, "digest": digest[:200],
        "digest_path": f"docs/{storage_path}/digest.md", "tags": [],
        "created_at": created, "content_hash": "sha256:abc",
        "extract_tables": extract_tables, "requested_url": requested_url or url,
        "lang": "en-US",
    }
    (docs_dir / "doc-index.json").write_text(
        json.dumps({"version": 2, "documents": [entry]}), encoding="utf-8"
    )
    doc_dir = docs_dir / storage_path
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "digest.md").write_text(f"# {doc_id}: Example\n\n{digest}\n", encoding="utf-8")
    return entry


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


def test_capture_closes_context_when_setup_fails(client: TestClient) -> None:
    """C12: if setup raises before the session manager takes ownership, the
    BrowserContext must be closed (not leaked)."""
    import mantisfetch_browser as lb

    mock_context = AsyncMock()
    mock_context.close = AsyncMock()
    orig_browser = lb._browser
    lb._browser = MagicMock()
    lb._browser.new_context = AsyncMock(return_value=mock_context)
    try:
        with (
            patch("mantisfetch_browser._setup_routing", new=AsyncMock(side_effect=RuntimeError("boom"))),
            pytest.raises(RuntimeError),
        ):
            client.post("/web/capture", json={"url": "https://example.com"})
    finally:
        lb._browser = orig_browser

    mock_context.close.assert_awaited()


def test_capture_persists_to_doc_library(client: TestClient) -> None:
    """POST /web/capture with a valid URL writes files and returns doc metadata."""
    distill_result = _make_distill_result()

    with tempfile.TemporaryDirectory() as tmp_dir:
        docs_dir = Path(tmp_dir)

        with (
            patch("mantisfetch_browser._get_docs_dir", return_value=docs_dir),
            patch("mantisfetch_browser._distill", new=AsyncMock(return_value=distill_result)),
            patch("mantisfetch_browser._browser", new=MagicMock()),
            patch("mantisfetch_browser._setup_routing", new=AsyncMock()),
        ):
            # Mock the browser context/page creation chain
            mock_page = AsyncMock()
            mock_page.goto = AsyncMock()
            mock_context = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)

            import mantisfetch_browser as lb
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


def test_find_cached_capture_hit_stale_and_filters(tmp_path: Path) -> None:
    """_find_cached_capture matches a recent (url, content_type) and rejects
    stale entries, other URLs, and other categories."""
    import mantisfetch_browser as lb

    _seed_capture_index(tmp_path, url="https://example.com", content_type="General", age_hours=1.0)
    en = "en-US"
    # within TTL
    hit = lb._find_cached_capture(tmp_path, "https://example.com", "General", True, en, 24.0)
    assert hit is not None and hit["id"] == "WEB-001"
    # older than TTL
    assert lb._find_cached_capture(tmp_path, "https://example.com", "General", True, en, 0.5) is None
    # different URL / content_type / extract_tables / lang
    assert lb._find_cached_capture(tmp_path, "https://other.com", "General", True, en, 24.0) is None
    assert lb._find_cached_capture(tmp_path, "https://example.com", "Knowledge", True, en, 24.0) is None
    assert lb._find_cached_capture(tmp_path, "https://example.com", "General", False, en, 24.0) is None
    assert lb._find_cached_capture(tmp_path, "https://example.com", "General", True, "zh-CN", 24.0) is None


def test_find_cached_capture_picks_most_recent(tmp_path: Path) -> None:
    import mantisfetch_browser as lb

    older = (datetime.now(UTC) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    newer = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    docs = [
        {"id": "WEB-001", "source": "web_capture", "source_url": "https://x.com",
         "content_type": "General", "created_at": older, "sections": 1, "tables": 0},
        {"id": "WEB-002", "source": "web_capture", "source_url": "https://x.com",
         "content_type": "General", "created_at": newer, "sections": 1, "tables": 0},
    ]
    (tmp_path / "doc-index.json").write_text(
        json.dumps({"version": 2, "documents": docs}), encoding="utf-8"
    )
    hit = lb._find_cached_capture(tmp_path, "https://x.com", "General", True, "en-US", 24.0)
    assert hit is not None and hit["id"] == "WEB-002"


def test_capture_reuses_recent_capture(client: TestClient) -> None:
    """With TTL enabled, a repeat capture of the same URL returns the cached
    doc_id without browsing (reused=True)."""
    # > 200 chars so we can prove the full digest (from digest.md) is returned,
    # not the truncated index preview.
    long_digest = "Quarterly revenue analysis across regions. " * 8
    assert len(long_digest) > 200
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        _seed_capture_index(
            docs_dir, doc_id="WEB-005", url="https://example.com",
            content_type="Knowledge", age_hours=2.0, digest=long_digest,
        )
        with (
            patch("mantisfetch_browser._get_docs_dir", return_value=docs_dir),
            patch("mantisfetch_browser.CAPTURE_TTL_HOURS", 24.0),
        ):
            resp = client.post(
                "/web/capture",
                json={"url": "https://example.com", "content_type": "Knowledge"},
            )
    assert resp.status_code == 200
    data = resp.json()
    assert data["reused"] is True
    assert data["doc_id"] == "WEB-005"
    assert data["cache_age_hours"] is not None and data["cache_age_hours"] >= 0
    # full digest (from digest.md), not the 200-char index preview
    assert data["digest"] == long_digest.strip()
    assert len(data["digest"]) > 200
    # section_count matches a fresh response (text sections + table sections)
    assert data["section_count"] == 4
    assert data["table_count"] == 1


def test_capture_reuses_across_redirect(client: TestClient) -> None:
    """The dedup key is the caller-supplied URL, so a URL whose capture was stored
    under a post-redirect source_url still hits the cache on repeat."""
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        _seed_capture_index(
            docs_dir, doc_id="WEB-007", url="https://example.com/",  # post-redirect
            content_type="General", age_hours=1.0, requested_url="http://example.com",
        )
        with (
            patch("mantisfetch_browser._get_docs_dir", return_value=docs_dir),
            patch("mantisfetch_browser.CAPTURE_TTL_HOURS", 24.0),
        ):
            resp = client.post("/web/capture", json={"url": "http://example.com"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["reused"] is True
    assert data["doc_id"] == "WEB-007"


def test_capture_extract_tables_mismatch_not_reused(client: TestClient) -> None:
    """A cached extract_tables=false capture must not be reused for a default
    (extract_tables=true) request — it re-captures instead."""
    distill_result = _make_distill_result()
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        _seed_capture_index(
            docs_dir, doc_id="WEB-001", url="https://example.com",
            content_type="Knowledge", age_hours=1.0, extract_tables=False,
        )
        with (
            patch("mantisfetch_browser._get_docs_dir", return_value=docs_dir),
            patch("mantisfetch_browser.CAPTURE_TTL_HOURS", 24.0),
            patch("mantisfetch_browser._distill", new=AsyncMock(return_value=distill_result)),
            patch("mantisfetch_browser._setup_routing", new=AsyncMock()),
        ):
            mock_page = AsyncMock()
            mock_page.goto = AsyncMock()
            mock_context = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)
            import mantisfetch_browser as lb
            orig_browser = lb._browser
            lb._browser = MagicMock()
            lb._browser.new_context = AsyncMock(return_value=mock_context)
            try:
                resp = client.post(
                    "/web/capture",
                    json={"url": "https://example.com", "content_type": "Knowledge"},
                )
            finally:
                lb._browser = orig_browser
    assert resp.status_code == 200
    assert resp.json()["reused"] is False


def test_capture_force_refresh_bypasses_cache(client: TestClient) -> None:
    """force_refresh=true re-captures even when a fresh cache entry exists."""
    distill_result = _make_distill_result()
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        _seed_capture_index(
            docs_dir, doc_id="WEB-001", url="https://example.com",
            content_type="Knowledge", age_hours=1.0, digest="cached digest",
        )
        with (
            patch("mantisfetch_browser._get_docs_dir", return_value=docs_dir),
            patch("mantisfetch_browser.CAPTURE_TTL_HOURS", 24.0),
            patch("mantisfetch_browser._distill", new=AsyncMock(return_value=distill_result)),
            patch("mantisfetch_browser._setup_routing", new=AsyncMock()),
        ):
            mock_page = AsyncMock()
            mock_page.goto = AsyncMock()
            mock_context = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)
            import mantisfetch_browser as lb
            orig_browser = lb._browser
            lb._browser = MagicMock()
            lb._browser.new_context = AsyncMock(return_value=mock_context)
            try:
                resp = client.post(
                    "/web/capture",
                    json={
                        "url": "https://example.com",
                        "content_type": "Knowledge",
                        "force_refresh": True,
                    },
                )
            finally:
                lb._browser = orig_browser
    assert resp.status_code == 200
    data = resp.json()
    assert data["reused"] is False
    # actually browsed: digest comes from the fresh distill, not the cached entry
    assert data["digest"] != "cached digest"


def test_capture_browser_not_ready(client: TestClient) -> None:
    """POST /web/capture returns 500 when the browser is not initialised."""
    import mantisfetch_browser as lb

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

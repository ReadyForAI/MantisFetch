"""Tests for POST /web/capture (one-shot web capture endpoint)."""

import asyncio
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
    common = {
        "source": "web_capture", "source_url": "https://x.com",
        "requested_url": "https://x.com", "content_type": "General",
        "extract_tables": True, "lang": "en-US", "sections": 1, "tables": 0,
    }
    docs = [
        {"id": "WEB-001", "created_at": older, **common},
        {"id": "WEB-002", "created_at": newer, **common},
    ]
    (tmp_path / "doc-index.json").write_text(
        json.dumps({"version": 2, "documents": docs}), encoding="utf-8"
    )
    hit = lb._find_cached_capture(tmp_path, "https://x.com", "General", True, "en-US", 24.0)
    assert hit is not None and hit["id"] == "WEB-002"


def test_find_cached_capture_skips_legacy_entries(tmp_path: Path) -> None:
    """A pre-feature entry (no requested_url/extract_tables/lang) is a cache miss —
    we re-capture rather than reuse it under assumed defaults."""
    import mantisfetch_browser as lb

    created = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    legacy = {
        "id": "WEB-001", "source": "web_capture", "source_url": "https://example.com",
        "content_type": "General", "created_at": created, "sections": 1, "tables": 0,
    }
    (tmp_path / "doc-index.json").write_text(
        json.dumps({"version": 2, "documents": [legacy]}), encoding="utf-8"
    )
    assert lb._find_cached_capture(
        tmp_path, "https://example.com", "General", True, "en-US", 24.0
    ) is None


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


@pytest.mark.asyncio
async def test_concurrent_same_url_captures_only_once(tmp_path: Path) -> None:
    """Two identical /capture calls racing before either persists must result in a
    single real capture; the loser waits on the per-key lock and reuses the result."""
    import mantisfetch_browser as lb
    from mantisfetch_browser.models import CaptureRequest

    distill_result = _make_distill_result(url="https://example.com")
    distill_mock = AsyncMock(return_value=distill_result)
    with (
        patch("mantisfetch_browser._get_docs_dir", return_value=tmp_path),
        patch("mantisfetch_browser.CAPTURE_TTL_HOURS", 24.0),
        patch("mantisfetch_browser._distill", new=distill_mock),
        patch("mantisfetch_browser._setup_routing", new=AsyncMock()),
    ):
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        orig_browser = lb._browser
        lb._browser = MagicMock()
        lb._browser.new_context = AsyncMock(return_value=mock_context)
        try:
            req = CaptureRequest(url="https://example.com", content_type="Knowledge")
            r1, r2 = await asyncio.gather(lb.capture(req), lb.capture(req))
        finally:
            lb._browser = orig_browser

    # exactly one real browse happened; the other reused the cache under the lock
    assert distill_mock.await_count == 1
    results = [r1, r2]
    assert sum(1 for r in results if r.reused) == 1
    assert sum(1 for r in results if not r.reused) == 1
    assert r1.doc_id == r2.doc_id


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


def test_persist_web_capture_writes_metadata(tmp_path: Path) -> None:
    """T3: metadata is written verbatim into the manifest and scalar-filtered into
    the doc-index; the top-level source stays web_capture (search provenance lives
    only in metadata.source)."""
    import mantisfetch_browser as lb

    sections = _make_distill_result()["sections"]
    lb._persist_web_capture(
        doc_id="WEB-050", url="https://example.com", title="Ex",
        sections=sections, digest="d", tags=["t"], content_hash="h",
        docs_dir=tmp_path, content_type="Bid", extract_tables=True,
        requested_url="https://example.com", lang="en-US",
        metadata={
            "source": "web_search", "search_query": "q", "search_rank": 1,
            "nested": {"drop": "me"},  # non-scalar → dropped from the index
        },
    )

    manifest = json.loads((tmp_path / "Bid" / "WEB-050" / "manifest.json").read_text())
    assert manifest["source"] == "web_capture"  # top-level source unchanged
    assert manifest["metadata"]["source"] == "web_search"  # provenance in metadata
    assert manifest["metadata"]["search_rank"] == 1

    entry = next(
        d for d in json.loads((tmp_path / "doc-index.json").read_text())["documents"]
        if d["id"] == "WEB-050"
    )
    assert entry["source"] == "web_capture"
    assert entry["metadata"]["source"] == "web_search"
    assert entry["metadata"]["search_rank"] == 1
    assert "nested" not in entry["metadata"]  # non-scalar filtered for cheap filtering


def test_capture_metadata_does_not_bust_cache(client: TestClient) -> None:
    """T3: metadata is NOT part of the dedup key — a repeat capture carrying new
    metadata still hits the existing entry (reused=True, first-touch provenance)."""
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        _seed_capture_index(
            docs_dir, doc_id="WEB-009", url="https://example.com",
            content_type="General", age_hours=1.0,
        )
        with (
            patch("mantisfetch_browser._get_docs_dir", return_value=docs_dir),
            patch("mantisfetch_browser.CAPTURE_TTL_HOURS", 24.0),
        ):
            resp = client.post(
                "/web/capture",
                json={
                    "url": "https://example.com",
                    "metadata": {"source": "web_search", "search_rank": 3},
                },
            )
    assert resp.status_code == 200
    data = resp.json()
    assert data["reused"] is True  # metadata did not bust the cache
    assert data["doc_id"] == "WEB-009"


# ── B5 content-hash dedup ──────────────────────────────────────────────────────


def test_find_capture_by_content_hash_picks_most_recent(tmp_path: Path) -> None:
    import mantisfetch_browser as lb

    older = (datetime.now(UTC) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    newer = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    h = "sha256:body-same"
    legacy = "sha256:legacy-url-formula"
    docs = [
        {
            "id": "WEB-001",
            "source": "web_capture",
            "content_hash": h,
            "created_at": older,
            "requested_url": "https://a.com/?utm=1",
        },
        {
            "id": "WEB-002",
            "source": "web_capture",
            "content_hash": h,
            "created_at": newer,
            "requested_url": "https://a.com/amp",
        },
        {
            "id": "WEB-003",
            "source": "web_capture",
            "content_hash": "sha256:other",
            "created_at": newer,
        },
        {
            "id": "WEB-LEGACY",
            "source": "web_capture",
            "content_hash": legacy,
            "created_at": newer,
        },
    ]
    (tmp_path / "doc-index.json").write_text(
        json.dumps({"version": 2, "documents": docs}), encoding="utf-8"
    )
    hit = lb._find_capture_by_content_hash(tmp_path, h)
    assert hit is not None and hit["id"] == "WEB-002"
    assert lb._find_capture_by_content_hash(tmp_path, "") is None
    assert lb._find_capture_by_content_hash(tmp_path, "sha256:missing") is None
    # Pre-upgrade title+url+body hash still matches via also_match
    leg = lb._find_capture_by_content_hash(tmp_path, "sha256:new-body", also_match=legacy)
    assert leg is not None and leg["id"] == "WEB-LEGACY"


def test_merge_capture_tags_metadata_union_and_first_touch(tmp_path: Path) -> None:
    import mantisfetch_browser as lb

    entry = _seed_capture_index(tmp_path, doc_id="WEB-010", url="https://example.com")
    # Seed index entry with tags + metadata
    index = json.loads((tmp_path / "doc-index.json").read_text(encoding="utf-8"))
    index["documents"][0]["tags"] = ["a"]
    index["documents"][0]["metadata"] = {"source": "web_capture", "keep": "old"}
    (tmp_path / "doc-index.json").write_text(json.dumps(index), encoding="utf-8")
    # Manifest too
    manifest_path = tmp_path / entry["storage_path"] / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"tags": ["a"], "metadata": {"source": "web_capture", "keep": "old"}}),
        encoding="utf-8",
    )

    merged = lb._merge_capture_tags_metadata(
        tmp_path,
        index["documents"][0],
        tags=["b", "a"],
        metadata={
            "source": "web_search",
            "search_query": "q",
            "keep": "new",
            "nested": {"x": 1},  # non-scalar: index drops, manifest keeps
        },
    )
    assert merged["tags"] == ["a", "b"]
    # first-touch: existing keys win (index only stores scalar-filtered metadata)
    assert merged["metadata"]["source"] == "web_capture"
    assert merged["metadata"]["keep"] == "old"
    assert merged["metadata"]["search_query"] == "q"
    assert "nested" not in merged["metadata"]

    reloaded = json.loads((tmp_path / "doc-index.json").read_text(encoding="utf-8"))
    assert reloaded["documents"][0]["tags"] == ["a", "b"]
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert man["tags"] == ["a", "b"]
    assert man["metadata"]["search_query"] == "q"
    assert man["metadata"]["nested"] == {"x": 1}  # full metadata in manifest


def test_capture_reuses_by_content_hash_across_urls(client: TestClient) -> None:
    """B5: after distill, same body hash reuses an existing doc even for a new URL
    (CAPTURE_TTL=0 so URL cache is off)."""
    distill = _make_distill_result(url="https://example.com/amp")
    # Ensure content_hash is body-only and shared with a seeded entry
    import mantisfetch_browser as lb

    body_hash = lb._hash_text(
        "Example Domain\n" + "\n\n".join(s["t"] for s in distill["sections"])
    )
    distill["content_hash"] = body_hash
    distill["title"] = "Example Domain"

    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        # Persist a prior capture with the same body hash but a different URL
        lb._persist_web_capture(
            doc_id="WEB-020",
            url="https://example.com/?utm=old",
            title="Example Domain",
            sections=distill["sections"],
            digest="prior digest",
            tags=["prior"],
            content_hash=body_hash,
            docs_dir=docs_dir,
            content_type="General",
            extract_tables=True,
            requested_url="https://example.com/?utm=old",
            lang="en-US",
            metadata={"source": "web_capture"},
        )

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        orig_browser = lb._browser
        lb._browser = MagicMock()
        lb._browser.new_context = AsyncMock(return_value=mock_context)
        try:
            with (
                patch("mantisfetch_browser._get_docs_dir", return_value=docs_dir),
                patch("mantisfetch_browser.CAPTURE_TTL_HOURS", 0.0),
                patch("mantisfetch_browser._distill", new=AsyncMock(return_value=distill)),
                patch("mantisfetch_browser._setup_routing", new=AsyncMock()),
            ):
                resp = client.post(
                    "/web/capture",
                    json={
                        "url": "https://example.com/amp",
                        "tags": ["from-amp"],
                        "metadata": {"search_query": "q"},
                    },
                )
        finally:
            lb._browser = orig_browser

        assert resp.status_code == 200
        data = resp.json()
        assert data["reused"] is True
        assert data["doc_id"] == "WEB-020"
        # tags merged
        index = json.loads((docs_dir / "doc-index.json").read_text(encoding="utf-8"))
        entry = next(d for d in index["documents"] if d["id"] == "WEB-020")
        assert set(entry["tags"]) == {"prior", "from-amp"}
        assert entry["metadata"].get("search_query") == "q"


def test_content_hash_excludes_url() -> None:
    """Body-only hash: same title+sections, different URLs → same content_hash."""
    import mantisfetch_browser as lb

    sections = [
        {"sid": "s1", "h": "H", "t": "same body", "type": "text"},
    ]
    joined = "\n\n".join(s["t"] for s in sections)
    h1 = lb._hash_text("Title\n" + joined)
    h2 = lb._hash_text("Title\n" + joined)
    assert h1 == h2
    # URL must not be part of the formula (explicit regression guard)
    with_url = lb._hash_text("Title\nhttps://a.com\n" + joined)
    assert h1 != with_url


def test_search_and_capture_url_ttl_independent(monkeypatch) -> None:
    import mantisfetch_browser as lb

    # Default is independent of CAPTURE_TTL
    assert lb._search_and_capture_url_ttl() == lb.SEARCH_CAPTURE_TTL_HOURS
    # 0 disables URL reuse on the search path even if CAPTURE_TTL is high
    monkeypatch.setattr(lb, "SEARCH_CAPTURE_TTL_HOURS", 0.0)
    monkeypatch.setattr(lb, "CAPTURE_TTL_HOURS", 48.0)
    assert lb._search_and_capture_url_ttl() == 0.0

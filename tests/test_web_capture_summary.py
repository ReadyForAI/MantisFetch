"""D4: /web/capture summary_mode=defer generates an LLM digest + brief in the
background (three-tier parity with /doc). Opt-in; default 'off' stays cheap."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import mantisfetch_browser as mb
from starlette.testclient import TestClient

_SECTIONS = [
    {"sid": "s_001", "h": "Intro", "t": "Intro body text.", "type": "text"},
    {"sid": "s_002", "h": "Details", "t": "Details body text.", "type": "text"},
    {"sid": "t_001", "h": "Table", "t": "a | b", "type": "table", "table_meta": {"rows": 1}},
]


def _persist(docs_dir: Path, summary_mode: str, doc_id: str = "WEB-001") -> Path:
    mb._persist_web_capture(
        doc_id=doc_id,
        url="https://example.com",
        title="Example",
        sections=_SECTIONS,
        digest="snippet digest",
        tags=["t"],
        content_hash="sha256:abc",
        docs_dir=docs_dir,
        content_type="General",
        summary_mode=summary_mode,
    )
    return docs_dir / "General" / doc_id


def test_off_mode_writes_no_summary(tmp_path: Path) -> None:
    doc_dir = _persist(tmp_path, "off")
    manifest = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "summary" not in manifest.get("parse_metadata", {})
    assert not (doc_dir / "brief.md").exists()


def test_defer_persist_marks_pending(tmp_path: Path) -> None:
    doc_dir = _persist(tmp_path, "defer")
    manifest = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parse_metadata"]["summary"]["status"] == "pending"


def test_defer_summary_writes_brief_and_llm_digest(tmp_path: Path, monkeypatch) -> None:
    import mantisfetch_docreader as dr

    doc_dir = _persist(tmp_path, "defer")
    monkeypatch.setattr(
        dr, "generate_summaries", lambda parsed, c, f: ("LLM DIGEST", "LLM BRIEF", [])
    )

    mb._defer_web_summary("WEB-001", _SECTIONS, tmp_path, "General", "Example", "https://example.com")

    assert "LLM BRIEF" in (doc_dir / "brief.md").read_text(encoding="utf-8")
    assert "LLM DIGEST" in (doc_dir / "digest.md").read_text(encoding="utf-8")
    manifest = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parse_metadata"]["summary"]["status"] == "completed"
    assert manifest["paths"]["brief"] == "brief.md"
    index = json.loads((tmp_path / "doc-index.json").read_text(encoding="utf-8"))
    assert index["documents"][0]["digest"] == "LLM DIGEST"


def test_defer_summary_failure_marks_failed(tmp_path: Path, monkeypatch) -> None:
    import mantisfetch_docreader as dr

    doc_dir = _persist(tmp_path, "defer")

    def boom(*args, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(dr, "generate_summaries", boom)
    mb._defer_web_summary("WEB-001", _SECTIONS, tmp_path, "General", "Example", "https://example.com")

    manifest = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parse_metadata"]["summary"]["status"] == "failed"
    assert not (doc_dir / "brief.md").exists()  # keep the snippet digest, no half-written brief


def _distill_result() -> dict:
    return {
        "url": "https://example.com",
        "title": "Example",
        "content_hash": "sha256:abc123",
        "sections": _SECTIONS,
        "actions": [],
        "meta": {},
    }


def test_capture_endpoint_schedules_defer_summary(client: TestClient, monkeypatch) -> None:
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "generate_summaries", lambda parsed, c, f: ("EP DIGEST", "EP BRIEF", []))

    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        with (
            patch("mantisfetch_browser._get_docs_dir", return_value=docs_dir),
            patch("mantisfetch_browser._distill", new=AsyncMock(return_value=_distill_result())),
            patch("mantisfetch_browser._setup_routing", new=AsyncMock()),
        ):
            mock_page = AsyncMock()
            mock_page.goto = AsyncMock()
            mock_context = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)
            orig_browser = mb._browser
            mb._browser = MagicMock()
            mb._browser.new_context = AsyncMock(return_value=mock_context)
            try:
                resp = client.post(
                    "/web/capture", json={"url": "https://example.com", "summary_mode": "defer"}
                )
            finally:
                mb._browser = orig_browser

            assert resp.status_code == 200
            data = resp.json()
            assert data["summary_status"] == "pending"

            manifest_path = docs_dir / data["storage_path"] / "manifest.json"
            for _ in range(60):  # wait for the background summary thread
                status = (
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                    .get("parse_metadata", {})
                    .get("summary", {})
                    .get("status")
                )
                if status == "completed":
                    break
                time.sleep(0.05)
            assert status == "completed"
            assert "EP BRIEF" in (manifest_path.parent / "brief.md").read_text(encoding="utf-8")

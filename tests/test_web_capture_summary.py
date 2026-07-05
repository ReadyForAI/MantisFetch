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


def test_reload_web_capture_text_sections_roundtrip(tmp_path: Path) -> None:
    doc_dir = _persist(tmp_path, "off")
    reloaded = mb._load_web_capture_text_sections(doc_dir)
    assert [s["sid"] for s in reloaded] == ["s_001", "s_002"]
    assert reloaded[0]["h"] == "Intro"
    assert "Intro body text." in reloaded[0]["t"]


_CACHED_ENTRY = {
    "id": "WEB-001",
    "storage_path": "General/WEB-001",
    "content_type": "General",
    "filename": "Example",
    "source_url": "https://example.com",
}


def test_cache_hit_defer_schedules_when_no_summary(tmp_path: Path, monkeypatch) -> None:
    import mantisfetch_docreader as dr

    _persist(tmp_path, "off")  # a cached doc that has no summary yet
    monkeypatch.setattr(dr, "generate_summaries", lambda parsed, c, f: ("HIT DIGEST", "HIT BRIEF", []))

    status = mb._resolve_cached_summary(_CACHED_ENTRY, tmp_path, "General", "defer")
    assert status == "pending"

    doc_dir = tmp_path / "General" / "WEB-001"
    for _ in range(60):
        m = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))
        if m.get("parse_metadata", {}).get("summary", {}).get("status") == "completed":
            break
        time.sleep(0.05)
    assert (doc_dir / "brief.md").read_text(encoding="utf-8").strip() == "HIT BRIEF"


def test_cache_hit_defer_reports_existing_without_rescheduling(tmp_path: Path) -> None:
    doc_dir = _persist(tmp_path, "defer")
    mb._set_web_summary_status(doc_dir, "completed", add_brief_path=True)
    # No generate_summaries mock: if this wrongly rescheduled it would try the
    # real pipeline. It must just report the existing status.
    assert mb._resolve_cached_summary(_CACHED_ENTRY, tmp_path, "General", "defer") == "completed"


def test_cache_hit_off_mode_reports_no_status(tmp_path: Path) -> None:
    _persist(tmp_path, "defer")
    assert mb._resolve_cached_summary(_CACHED_ENTRY, tmp_path, "General", "off") is None


def test_cache_hit_defer_claims_and_dedups(tmp_path: Path, monkeypatch) -> None:
    """Concurrent cache hits must enqueue exactly one LLM job (atomic claim)."""
    import threading as _threading

    import mantisfetch_docreader as dr

    _persist(tmp_path, "off")
    calls: list[int] = []
    gate = _threading.Event()

    def slow(parsed, c, f):
        calls.append(1)
        gate.wait(2.0)
        return ("D", "B", [])

    monkeypatch.setattr(dr, "generate_summaries", slow)
    doc_dir = tmp_path / "General" / "WEB-001"

    assert mb._resolve_cached_summary(_CACHED_ENTRY, tmp_path, "General", "defer") == "pending"
    # claimed synchronously (the worker is blocked on `gate`, not yet done)
    assert mb._read_web_summary_status(doc_dir) in {"pending", "running"}
    # a second hit in the window must not enqueue another job
    assert mb._resolve_cached_summary(_CACHED_ENTRY, tmp_path, "General", "defer") in {
        "pending",
        "running",
    }

    gate.set()
    for _ in range(60):
        if mb._read_web_summary_status(doc_dir) == "completed":
            break
        time.sleep(0.05)
    time.sleep(0.1)  # let any (wrongly) queued duplicate run
    assert calls == [1], "duplicate summary jobs enqueued on concurrent cache hits"


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

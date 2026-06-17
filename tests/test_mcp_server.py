"""Tests for the MantisFetch MCP server: tool registry, the doc_parse allowlist
root guard, the web injection-boundary wrap, source validation, and delegation.

No real browser or MCP transport is started — tools are exercised directly and
the /web /doc apps are stubbed at the delegation-helper seam.
"""

import asyncio
import base64
from unittest.mock import AsyncMock

import mantisfetch_mcp as mm
import pytest

EXPECTED_TOOLS = {
    # web (9)
    "web_capture", "web_session_open", "web_goto", "web_distill", "web_read_sections",
    "web_act", "web_scroll", "web_navigate", "web_session_close",
    # doc (12)
    "doc_parse", "doc_digest", "doc_brief", "doc_sections", "doc_section", "doc_full",
    "doc_search", "doc_search_sections", "doc_table", "doc_chunks", "doc_manifest",
    "doc_summary",
}


def test_tool_registry_matches_contract() -> None:
    names = {t.name for t in asyncio.run(mm.mcp.list_tools())}
    assert names == EXPECTED_TOOLS
    assert len(names) <= 40  # NodalOS mcp.products[].max_tools cap


# ── injection boundary ─────────────────────────────────────────────────────────


def test_wrap_web_result_wraps_text_fields() -> None:
    result = {
        "url": "https://evil.example/page",
        "digest": "summary text",
        "sections": [{"sid": "s1", "t": "body text"}, {"sid": "s2", "t": "more"}],
    }
    out = mm._wrap_web_result(result, "https://evil.example/page")
    assert out["sections"][0]["t"].startswith("⟦mantisfetch:web-content nonce=")
    assert "origin=https://evil.example/page" in out["sections"][0]["t"]
    assert "body text" in out["sections"][0]["t"]
    assert out["digest"].startswith("⟦mantisfetch:web-content")
    # same response → same nonce across its fields
    n0 = out["sections"][0]["t"].split("nonce=")[1].split(" ")[0]
    n1 = out["sections"][1]["t"].split("nonce=")[1].split(" ")[0]
    assert n0 == n1


def test_wrap_web_result_passthrough_non_dict() -> None:
    assert mm._wrap_web_result("plain", "o") == "plain"


# ── doc_parse allowlist root (①) ───────────────────────────────────────────────


def test_resolve_local_doc_within_root(tmp_path, monkeypatch) -> None:
    root = tmp_path / "resource"
    root.mkdir()
    (root / "a.pdf").write_bytes(b"%PDF-1.4 data")
    monkeypatch.setenv("MANTISFETCH_ALLOWED_DOC_ROOTS", str(root))
    name, data = mm._resolve_local_doc("a.pdf")
    assert name == "a.pdf"
    assert data == b"%PDF-1.4 data"


def test_resolve_local_doc_rejects_traversal(tmp_path, monkeypatch) -> None:
    root = tmp_path / "resource"
    root.mkdir()
    (tmp_path / "secret.txt").write_bytes(b"top secret")
    monkeypatch.setenv("MANTISFETCH_ALLOWED_DOC_ROOTS", str(root))
    with pytest.raises(mm.ToolError):
        mm._resolve_local_doc("../secret.txt")


def test_resolve_local_doc_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    root = tmp_path / "resource"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"escaped")
    (root / "link.txt").symlink_to(outside)  # canonical path lands outside root
    monkeypatch.setenv("MANTISFETCH_ALLOWED_DOC_ROOTS", str(root))
    with pytest.raises(mm.ToolError):
        mm._resolve_local_doc("link.txt")


def test_resolve_local_doc_rejects_absolute(tmp_path, monkeypatch) -> None:
    root = tmp_path / "resource"
    root.mkdir()
    monkeypatch.setenv("MANTISFETCH_ALLOWED_DOC_ROOTS", str(root))
    with pytest.raises(mm.ToolError):
        mm._resolve_local_doc("/etc/passwd")


def test_resolve_local_doc_disabled_without_root(monkeypatch) -> None:
    monkeypatch.delenv("MANTISFETCH_ALLOWED_DOC_ROOTS", raising=False)
    with pytest.raises(mm.ToolError, match="disabled"):
        mm._resolve_local_doc("a.pdf")


# ── doc_parse source validation ────────────────────────────────────────────────


def test_doc_parse_requires_exactly_one_source() -> None:
    with pytest.raises(mm.ToolError, match="exactly one"):
        asyncio.run(mm.doc_parse())
    with pytest.raises(mm.ToolError, match="exactly one"):
        asyncio.run(mm.doc_parse(rel_path="a.pdf", url="http://x/a.pdf"))


def test_doc_parse_rejects_bad_base64() -> None:
    with pytest.raises(mm.ToolError, match="base64"):
        asyncio.run(mm.doc_parse(content_b64="not!base64!", filename="a.pdf"))


def test_doc_parse_base64_requires_filename() -> None:
    good = base64.b64encode(b"data").decode()
    with pytest.raises(mm.ToolError, match="filename"):
        asyncio.run(mm.doc_parse(content_b64=good))


# ── delegation + wrapping (web tool over a stubbed transport) ───────────────────


def test_web_distill_delegates_and_wraps(monkeypatch) -> None:
    fake = {
        "url": "https://site.example",
        "sections": [{"sid": "s1", "t": "hello"}],
        "actions": [],
        "meta": {},
    }
    monkeypatch.setattr(mm, "_web_post", AsyncMock(return_value=fake))
    out = asyncio.run(mm.web_distill("SID-1"))
    mm._web_post.assert_awaited_once()
    # path + payload threaded through
    args, _ = mm._web_post.call_args
    assert args[0] == "/session/distill"
    assert args[1]["session_id"] == "SID-1"
    # untrusted text wrapped
    assert out["sections"][0]["t"].startswith("⟦mantisfetch:web-content")


def test_unwrap_raises_tool_error_on_4xx() -> None:
    import httpx

    resp = httpx.Response(409, json={"detail": "occluded by div#overlay"})
    with pytest.raises(mm.ToolError, match="occluded by div#overlay"):
        mm._unwrap(resp)

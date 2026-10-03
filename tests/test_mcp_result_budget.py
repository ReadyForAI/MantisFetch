"""Tool results that fit in what an MCP client delivers (#290).

NodalOS cuts every tool return at 65,536 bytes, mid-string. A 162-page OCR scan
(446 sections, 119 tables) has a 509 KB manifest, and doc_manifest delivered
about one ninth of it — JSON that would not parse, or a section list that
looked complete and was not. doc_sections, where a caller goes next, was 183 KB
for the same document.

These go through the real /mcp app on the 2026-07-28 face, which escapes every
non-ASCII character to \\uXXXX on the wire, and measure the envelope bytes.
"""

from __future__ import annotations

import importlib
import json
from contextlib import asynccontextmanager

import mantisfetch_mcp as mm
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

WALL = 65_536
_MODERN = "2026-07-28"
_META = {
    "io.modelcontextprotocol/protocolVersion": _MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _big_document(n_sections: int = 500, n_tables: int = 100) -> dict:
    sections = [
        {
            "index": i, "sid": f"s_{i:04d}", "title": f"第{i}章 合同条款与付款安排（续）",
            "type": "text", "page_start": i // 3 + 1, "page_end": i // 3 + 1,
            "page_range": f"p.{i // 3 + 1}", "char_count": 1800, "token_estimate": 900,
            "summary_preview": "本节约定了付款节点、验收标准以及违约责任的计算方式。" * 2,
            "table_refs": [f"t_{i:04d}"] if i < n_tables else [],
            "text_hash": "sha256:" + "a" * 64, "file": f"sections/{i:04d}.md",
        }
        for i in range(n_sections)
    ]
    tables = [
        {"table_id": f"t_{i:04d}", "caption": f"表{i} 付款明细", "rows": 40, "cols": 8,
         "columns": [{"name": f"列{c}", "type": "number", "min": 0, "max": 99999} for c in range(8)]}
        for i in range(n_tables)
    ]
    return {
        "doc_id": "DOC-9290", "filename": "大型扫描合同.pdf", "file_type": "pdf",
        "content_type": "General", "storage_path": "General/DOC-9290", "kind": "parsed",
        "total_pages": 162, "section_count": n_sections, "table_count": n_tables, "image_count": 0,
        "sections": sections, "tables": tables, "images": [],
        "parse_metadata": {
            "quality_assessment": {"pages": [{"page": p, "score": 0.81, "issues": ["低对比度"]}
                                             for p in range(1, 163)]},
            "summary": {"status": "completed"},
        },
        "provenance": {"source": "upload", "content_hash": "sha256:" + "b" * 64},
    }


@pytest.fixture(scope="module")
def client():
    importlib.reload(mm)

    @asynccontextmanager
    async def lifespan(app):
        async with mm.mcp.session_manager.run():
            yield

    app = Starlette(lifespan=lifespan, routes=[Mount("/mcp", app=mm.mcp_app)])
    with TestClient(app, base_url="http://127.0.0.1:9898", client=("127.0.0.1", 45678)) as c:
        yield c
    importlib.reload(mm)


@pytest.fixture()
def library(monkeypatch):
    docs: dict[str, dict] = {}

    async def fake_get(path, params=None):
        doc_id, face = path.split("/")[2], path.split("/")[3]
        doc = docs[doc_id]
        if face == "manifest":
            return json.loads(json.dumps(doc))
        if face == "sections":
            return {"doc_id": doc_id, "kind": "parsed", "sections": doc["sections"]}
        raise AssertionError(path)

    monkeypatch.setattr(mm, "_doc_get", fake_get)
    return docs


def _call(client, tool: str, arguments: dict) -> tuple[dict, int]:
    resp = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": _MODERN, "mcp-method": "tools/call", "mcp-name": tool},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments, "_meta": _META}},
    )
    text = resp.text
    if "data: " in text:
        text = next(line[6:] for line in text.splitlines() if line.startswith("data: "))
    result = json.loads(text)["result"]
    return result, len(text.encode("utf-8"))


def _payload(result: dict) -> dict:
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_a_large_manifest_arrives_whole_as_a_projection(client, library) -> None:
    library["DOC-9290"] = _big_document()
    result, size = _call(client, "doc_manifest", {"doc_id": "DOC-9290"})

    assert size < WALL, f"envelope is {size} bytes — past the wall"
    manifest = _payload(result)
    assert manifest["truncated"] is True
    assert {"sections", "tables", "parse_metadata.quality_assessment"} <= set(manifest["omitted"])
    assert (manifest["section_count"], manifest["table_count"]) == (500, 100)
    assert manifest["provenance"]["content_hash"].startswith("sha256:")
    assert "doc_sections" in manifest["see"]


def test_walking_the_section_pages_returns_every_section_once(client, library) -> None:
    library["DOC-9290"] = _big_document()
    seen: list[str] = []
    offset, pages = 0, 0
    while offset is not None:
        result, size = _call(client, "doc_sections", {"doc_id": "DOC-9290", "offset": offset})
        assert size < WALL, f"page at offset {offset} is {size} bytes"
        page = _payload(result)
        assert page["total"] == 500 and page["offset"] == offset
        assert page["sections"], "an empty page would loop forever"
        assert page["truncated"] is (page["next_offset"] is not None)
        seen += [s["sid"] for s in page["sections"]]
        offset, pages = page["next_offset"], pages + 1
    assert seen == [f"s_{i:04d}" for i in range(500)]
    assert pages > 1


def test_a_small_document_is_unchanged_apart_from_the_new_fields(client, library) -> None:
    library["DOC-1"] = _big_document(n_sections=5, n_tables=1)
    manifest = _payload(_call(client, "doc_manifest", {"doc_id": "DOC-1"})[0])
    assert manifest["truncated"] is False
    assert len(manifest["sections"]) == 5 and "omitted" not in manifest

    page = _payload(_call(client, "doc_sections", {"doc_id": "DOC-1"})[0])
    assert len(page["sections"]) == 5
    assert (page["total"], page["next_offset"], page["truncated"]) == (5, None, False)


def test_paging_edges(client, library) -> None:
    library["DOC-1"] = _big_document(n_sections=5, n_tables=0)
    end = _payload(_call(client, "doc_sections", {"doc_id": "DOC-1", "offset": 5})[0])
    assert end["sections"] == [] and end["next_offset"] is None

    past, _ = _call(client, "doc_sections", {"doc_id": "DOC-1", "offset": 6})
    assert past.get("isError"), "an offset past the end must not quietly restart at 0"

    two = _payload(_call(client, "doc_sections", {"doc_id": "DOC-1", "offset": 1, "limit": 2})[0])
    assert [s["sid"] for s in two["sections"]] == ["s_0001", "s_0002"]
    assert (two["next_offset"], two["truncated"]) == (3, True)


def test_a_projection_keeps_every_table_id_reachable(client, library) -> None:
    """Codex round 1. A multi-section DOCX leaves every section's table_refs
    empty on purpose; with the tables list dropped, doc_table had no ids."""
    doc = _big_document()
    for section in doc["sections"]:
        section["table_refs"] = []
    library["DOC-9290"] = doc
    result, size = _call(client, "doc_manifest", {"doc_id": "DOC-9290"})

    assert size < WALL
    manifest = _payload(result)
    assert manifest["truncated"] is True
    assert manifest["table_ids"] == [f"t_{i:04d}" for i in range(100)]

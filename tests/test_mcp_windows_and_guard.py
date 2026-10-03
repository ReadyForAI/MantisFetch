"""Every MCP tool result fits in what the client delivers (#315, #314).

#312 bounded doc_manifest, doc_sections and doc_tables. Everything else had no
limit: doc_full of a 162-page audit report was 406,672 bytes on the wire, a
full doc_source window of Chinese 132,106 — and NodalOS cuts every tool return
at 65,536, mid-string, with isError false. doc_full, doc_table and doc_chunks
now come in windows/pages, doc_source fits its window to the wire, and any
other tool over the budget answers with a tool error that says how to ask.

Through the real /mcp app on the 2026-07-28 face, measuring envelope bytes.
"""

from __future__ import annotations

import importlib
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

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
_PARA = "本节约定付款节点、验收标准与违约责任，具体以附件为准。"


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
def served(monkeypatch):
    """What the document service answers, by path."""
    answers: dict[str, object] = {}

    async def fake_get(path, params=None):
        return json.loads(json.dumps(answers[path]))

    async def fake_post(path, payload, headers=None):
        return json.loads(json.dumps(answers[path]))

    monkeypatch.setattr(mm, "_doc_get", fake_get)
    monkeypatch.setattr(mm, "_doc_post", fake_post)
    return answers


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
    return json.loads(text)["result"], len(text.encode("utf-8"))


def _payload(result: dict) -> dict:
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def _walk(client, tool: str, args: dict, key: str) -> tuple[list, int]:
    """Follow next_offset to the end; every envelope must be under the wall."""
    pieces, offset, windows = [], 0, 0
    while offset is not None:
        result, size = _call(client, tool, {**args, "offset": offset})
        assert size < WALL, f"{tool} window at {offset} is {size} bytes"
        page = _payload(result)
        assert page["truncated"] is (page["next_offset"] is not None)
        pieces.append(page[key])
        offset, windows = page["next_offset"], windows + 1
    return pieces, windows


def test_a_long_document_is_read_whole_through_windows(client, served) -> None:
    text = "".join(f"## 第{i}节\n\n{_PARA * 6}\n\n" for i in range(400))
    served["/library/DOC-1/full"] = {"doc_id": "DOC-1", "content": text}

    pieces, windows = _walk(client, "doc_full", {"doc_id": "DOC-1"}, "content")
    assert windows > 1
    assert "".join(pieces) == text, "the windows do not add up to the document"


def test_one_enormous_line_is_split_rather_than_sent_whole(client, served) -> None:
    """OCR text and minified pages can be a single line of hundreds of KB."""
    text = _PARA * 6000
    served["/library/DOC-2/full"] = {"doc_id": "DOC-2", "content": text}

    pieces, windows = _walk(client, "doc_full", {"doc_id": "DOC-2"}, "content")
    assert windows > 1 and "".join(pieces) == text


def test_a_document_that_fits_comes_back_whole_as_before(client, served) -> None:
    served["/library/DOC-3/full"] = {"doc_id": "DOC-3", "content": "short text\n"}
    page = _payload(_call(client, "doc_full", {"doc_id": "DOC-3"})[0])
    assert page["content"] == "short text\n"
    assert (page["truncated"], page["next_offset"]) == (False, None)


def test_a_long_markdown_table_repeats_its_header_in_every_window(client, served) -> None:
    # As docreader and capture store it: a title and a blank line first.
    header = "# Table 1 (page 12)\n\n| 序号 | 项目 | 金额 |\n| --- | --- | ---: |\n"
    rows = "".join(f"| {i} | {_PARA} | {i * 100} |\n" for i in range(3000))
    served["/library/DOC-4/table/table-01"] = {
        "doc_id": "DOC-4", "table_id": "table-01", "content": header + rows}

    pieces, windows = _walk(client, "doc_table", {"doc_id": "DOC-4", "table_id": "table-01"},
                            "content")
    assert windows > 1
    assert all(p.startswith(header) for p in pieces), "a window lost the header row"
    assert "".join(p[len(header):] for p in pieces) == rows


def test_a_long_json_table_is_paged_by_rows(client, served) -> None:
    table = {"table_id": "table-02", "row_count": 3000, "column_count": 3,
             "rows": [{"row_index": i, "cells": [str(i), _PARA, str(i * 100)]}
                      for i in range(3000)]}
    served["/library/DOC-5/table/table-02/json"] = {
        "doc_id": "DOC-5", "table_id": "table-02", "table": table}

    pages, windows = _walk(client, "doc_table",
                           {"doc_id": "DOC-5", "table_id": "table-02", "fmt": "json"}, "table")
    assert windows > 1
    assert all(p["row_count"] == 3000 for p in pages), "the table's metadata went missing"
    assert [r["row_index"] for p in pages for r in p["rows"]] == list(range(3000))


def test_chunks_with_their_text_come_in_pages(client, served) -> None:
    chunks = [{"chunk_id": f"c{i}", "text": _PARA * 30} for i in range(300)]
    served["/library/DOC-6/chunks"] = {"doc_id": "DOC-6", "chunk_count": 300, "chunks": chunks}

    pages, windows = _walk(client, "doc_chunks", {"doc_id": "DOC-6", "include_text": True},
                           "chunks")
    assert windows > 1
    assert [c["chunk_id"] for p in pages for c in p] == [f"c{i}" for i in range(300)]


def test_any_other_tool_over_the_budget_refuses_and_says_how_to_ask(client, served) -> None:
    served["/library/DOC-7/sections/batch"] = {
        "doc_id": "DOC-7", "sections": [{"sid": f"s{i}", "text": _PARA * 200} for i in range(40)],
        "missing": []}
    result, size = _call(client, "doc_sections_batch",
                         {"doc_id": "DOC-7", "sids": [f"s{i}" for i in range(40)]})

    assert size < WALL
    assert result.get("isError") is True, "an oversized result went out as a success"
    message = result["content"][0]["text"]
    assert "MANTISFETCH_MCP_RESULT_BUDGET_BYTES" in message and "fewer sids" in message


def test_the_budget_is_one_setting(client, served, monkeypatch) -> None:
    """Raised, nothing splits; zero or nonsense is the default, never 'unlimited'."""
    text = "".join(f"## 第{i}节\n\n{_PARA * 6}\n\n" for i in range(200))
    served["/library/DOC-8/full"] = {"doc_id": "DOC-8", "content": text}

    monkeypatch.setenv("MANTISFETCH_MCP_RESULT_BUDGET_BYTES", "10000000")
    whole = _payload(_call(client, "doc_full", {"doc_id": "DOC-8"})[0])
    assert whole["content"] == text and whole["truncated"] is False

    for value in ("0", "lots"):
        monkeypatch.setenv("MANTISFETCH_MCP_RESULT_BUDGET_BYTES", value)
        result, size = _call(client, "doc_full", {"doc_id": "DOC-8"})
        assert size < WALL and _payload(result)["truncated"] is True


def test_guarding_does_not_change_any_tool_schema(client) -> None:
    resp = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": _MODERN, "mcp-method": "tools/list"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _META}},
    )
    text = resp.text if "data: " not in resp.text else next(
        line[6:] for line in resp.text.splitlines() if line.startswith("data: "))
    tools = {t["name"]: t for t in json.loads(text)["result"]["tools"]}
    assert set(tools["doc_section"]["inputSchema"]["properties"]) == {"doc_id", "sid"}
    assert {"offset", "limit"} <= set(tools["doc_full"]["inputSchema"]["properties"])


def test_a_full_window_of_a_chinese_original_fits(client, monkeypatch) -> None:
    """#314, through the real document service: the server's 64 KiB-of-UTF-8
    window was ~132 KB on the wire for Chinese."""
    import mantisfetch_docreader as dr

    import mantisfetch_common.storage as cs

    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", Path(tempfile.mkdtemp()))
    line = _PARA + "\n"
    original = (line * 4000).encode("utf-8")
    doc_id = TestClient(dr.app).post(
        "/parse", files={"file": ("合同正文.md", original, "text/markdown")},
        data={"store_only": "true"}).json()["doc_id"]

    pieces, offset = [], 0
    while offset is not None:
        result, size = _call(client, "doc_source", {"doc_id": doc_id, "offset": offset})
        assert size < WALL, f"window at {offset} is {size} bytes"
        window = _payload(result)
        pieces.append(window["text"])
        offset = window["next_offset"]
    assert "".join(pieces).encode("utf-8") == original


def test_a_bare_markdown_table_repeats_its_header_too(client, served) -> None:
    header = "| a | b |\n|---|---|\n"
    rows = "".join(f"| {i} | {_PARA * 3} |\n" for i in range(3000))
    served["/library/DOC-9/table/table-01"] = {
        "doc_id": "DOC-9", "table_id": "table-01", "content": header + rows}
    pieces, windows = _walk(client, "doc_table", {"doc_id": "DOC-9", "table_id": "table-01"},
                            "content")
    assert windows > 1 and all(p.startswith(header) for p in pieces)
    assert "".join(p[len(header):] for p in pieces) == rows


def test_long_table_rows_and_headers_are_never_split(client, served) -> None:
    """Codex round 2: lines over ~3,750 chars were split into pieces before the
    header was taken, so a wide header lost its separator and a long row could
    end one window and start the next as bare cell text."""
    header = "# Table 2 (page 3)\n\n| " + " | ".join(f"列{i}" * 60 for i in range(30)) + " |\n" + \
        "|" + "---|" * 30 + "\n"
    rows = "".join("| " + " | ".join([str(i)] + [_PARA * 4] * 29) + " |\n" for i in range(400))
    served["/library/DOC-10/table/table-01"] = {
        "doc_id": "DOC-10", "table_id": "table-01", "content": header + rows}

    pieces, windows = _walk(client, "doc_table", {"doc_id": "DOC-10", "table_id": "table-01"},
                            "content")
    assert windows > 1
    for piece in pieces:
        assert piece.startswith(header), "a window lost part of the header"
        body = piece[len(header):]
        assert all(line.startswith("| ") and line.endswith(" |") for line in body.splitlines()), \
            "a row was split across windows"
    assert "".join(p[len(header):] for p in pieces) == rows


def test_a_table_row_too_large_on_its_own_is_cut_and_marked(client, served) -> None:
    header = "| a | b |\n|---|---|\n"
    rows = "| 1 | short |\n| 2 | " + _PARA * 8000 + " |\n| 3 | short |\n"
    served["/library/DOC-11/table/table-01"] = {
        "doc_id": "DOC-11", "table_id": "table-01", "content": header + rows}
    seen, offset = [], 0
    while offset is not None:
        result, size = _call(client, "doc_table",
                             {"doc_id": "DOC-11", "table_id": "table-01", "offset": offset})
        assert size < WALL
        page = _payload(result)
        seen.append(page)
        offset = page["next_offset"]
    assert any(p.get("content_truncated") for p in seen), "the oversized row went unmarked"
    assert seen[-1]["content"].endswith("| 3 | short |\n"), "the rows after it were lost"


def test_an_oversized_distill_is_trimmed_not_refused(client, monkeypatch) -> None:
    """Codex round 2: refusing it after the browser service had taken it as the
    session's baseline left the next distill diffing against a snapshot the
    caller never received. Trimmed, it says what it left out."""
    sections = [{"sid": f"s{i:03d}", "h": f"第{i}节", "t": _PARA * 20} for i in range(60)]

    async def fake_web_post(path, payload, headers=None):
        return {"url": "https://example.com/page", "title": "长页面", "sections": sections,
                "actions": [], "changed_sids": [], "hash_changed": True}

    monkeypatch.setattr(mm, "_web_post", fake_web_post)
    result, size = _call(client, "web_distill", {"session_id": "sess-1"})

    assert size < WALL
    out = _payload(result)
    assert out["truncated"] is True
    kept = [s["sid"] for s in out["sections"]]
    assert kept and kept + out["omitted_sids"] == [f"s{i:03d}" for i in range(60)]


@pytest.mark.parametrize("chars", [200, 261, 300, 1000])
def test_a_trimmed_distill_still_fits_once_its_omissions_are_listed(
    client, monkeypatch, chars
) -> None:
    """Codex round 3: the omitted_sids list was added after the trim was
    measured, and could push a result that had just fitted back over (60,035
    bytes with 261-character sections), where the guard refused it."""
    sections = [{"sid": f"s{i:03d}", "h": "节", "t": "约" * chars} for i in range(400)]

    async def fake_web_post(path, payload, headers=None):
        return {"url": "https://example.com/p", "sections": sections, "actions": []}

    monkeypatch.setattr(mm, "_web_post", fake_web_post)
    result, size = _call(client, "web_distill", {"session_id": "sess-1"})
    assert size < WALL
    out = _payload(result)
    assert [s["sid"] for s in out["sections"]] + out["omitted_sids"] == \
        [f"s{i:03d}" for i in range(400)]


def test_a_header_too_large_for_a_window_is_cut_so_rows_still_get_through(client, served) -> None:
    """Codex round 4: a 9,000-character header made every window too large,
    limit=1 included, so the guard refused them all."""
    header = "| " + "表头" * 4500 + " | b |\n|---|---|\n"
    rows = "".join(f"| {i} | short |\n" for i in range(50))
    served["/library/DOC-12/table/table-01"] = {
        "doc_id": "DOC-12", "table_id": "table-01", "content": header + rows}
    result, size = _call(client, "doc_table",
                         {"doc_id": "DOC-12", "table_id": "table-01", "limit": 1})
    assert size < WALL
    page = _payload(result)
    assert page["header_truncated"] is True and page["content"].endswith("| 0 | short |\n")


@pytest.mark.parametrize("cell", [119_583, 119_620])
def test_a_cut_row_still_fits_with_its_marker_on(client, served, cell) -> None:
    """Codex round 4: content_truncated was added after the cut was measured,
    turning a 59,969-byte window into 60,001."""
    header = "| a | b |\n|---|---|\n"
    rows = "| 1 | " + "x" * cell + " |\n| 2 | y |\n"
    served["/library/DOC-13/table/table-01"] = {
        "doc_id": "DOC-13", "table_id": "table-01", "content": header + rows}
    result, size = _call(client, "doc_table", {"doc_id": "DOC-13", "table_id": "table-01"})
    assert size < WALL
    page = _payload(result)
    assert page["content_truncated"] is True and page["next_offset"] == 1


def test_a_large_header_is_kept_whole_when_the_table_fits(client, served) -> None:
    """Codex round 5: the header was cut whenever it took half the budget, even
    when the whole table fitted, and the cut dropped the separator."""
    header = "| " + "表头" * 2500 + " | b |\n|---|---|\n"
    content = header + "| 1 | short |\n"
    served["/library/DOC-14/table/table-01"] = {
        "doc_id": "DOC-14", "table_id": "table-01", "content": content}
    page = _payload(_call(client, "doc_table", {"doc_id": "DOC-14", "table_id": "table-01"})[0])
    assert page["content"] == content and "header_truncated" not in page


def test_a_header_that_must_be_cut_keeps_the_separator(client, served) -> None:
    header = "| " + "表头" * 6000 + " | b |\n|---|---|\n"
    served["/library/DOC-15/table/table-01"] = {
        "doc_id": "DOC-15", "table_id": "table-01", "content": header + "| 1 | short |\n"}
    result, size = _call(client, "doc_table", {"doc_id": "DOC-15", "table_id": "table-01"})
    assert size < WALL
    lines = _payload(result)["content"].splitlines()
    assert lines[1] == "|---|---|" and lines[2] == "| 1 | short |"


def test_large_json_table_metadata_is_cut_so_rows_can_be_read(client, served) -> None:
    """Codex round 5: header/caption/stats repeated on every page left even a
    one-row page over the budget."""
    table = {"table_id": "table-03", "caption": "说明" * 9000, "column_count": 2,
             "rows": [{"row_index": i, "cells": [str(i), "x"]} for i in range(50)]}
    served["/library/DOC-16/table/table-03/json"] = {
        "doc_id": "DOC-16", "table_id": "table-03", "table": table}
    result, size = _call(client, "doc_table",
                         {"doc_id": "DOC-16", "table_id": "table-03", "fmt": "json"})
    assert size < WALL
    page = _payload(result)
    assert page["table"].get("caption_truncated") is True
    assert [r["row_index"] for r in page["table"]["rows"]] == list(range(50))


def test_json_table_metadata_is_kept_whole_when_the_table_fits(client, served) -> None:
    """Codex round 6: metadata over half the budget was cut even when the whole
    table fitted, losing its header, counts and provenance."""
    table = {"table_id": "table-04", "header": ["表头" * 2250, "b"], "row_count": 1,
             "column_count": 2, "source": "web", "rows": [{"row_index": 0, "cells": ["1", "x"]}]}
    served["/library/DOC-17/table/table-04/json"] = {
        "doc_id": "DOC-17", "table_id": "table-04", "table": table}
    page = _payload(_call(client, "doc_table",
                          {"doc_id": "DOC-17", "table_id": "table-04", "fmt": "json"})[0])
    assert page["table"] == table

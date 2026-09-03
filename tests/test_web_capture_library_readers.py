"""Web captures must be readable by the /doc library the same way parsed docs are.

Three holes this pins shut:

1. ``_resolve_manifest_section_path`` only accepted ``sections/``, so the table
   entries a web capture declares in ``manifest["sections"]`` (pointing at
   ``tables/table-NN.md``) were silently skipped by search_sections, chunks and
   search_text — the tables were on disk and declared, but unreachable.
2. No ``full.md`` was written, so doc_full 404'd and search_text?scope=full could
   never match a capture.
3. No ``tables.json`` sidecar, so doc_table fmt=json 404'd for every capture.
"""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

# A token that exists ONLY inside the table body, never in a text section — the
# whole point is proving the readers reach table files.
TABLE_ONLY_TOKEN = "Zylobactor"


def _sections_with_table() -> list[dict]:
    return [
        {"sid": "s_0001", "h": "Intro", "t": "Ordinary prose about nothing.", "type": "text"},
        {
            "sid": "t_0001",
            "h": "[Table] Species",
            "t": (
                "| Name | Count |\n| --- | --- |\n"
                f"| {TABLE_ONLY_TOKEN} | 12 |\n| Other | 7 |"
            ),
            "type": "table",
            "table_meta": {"rows": 3, "cols": 2, "has_header": True, "truncated": False},
        },
    ]


@pytest.fixture
def captured(tmp_path: Path) -> Path:
    """Persist one capture (text section + table) into a scratch library."""
    import mantisfetch_browser as lb

    lb._persist_web_capture(
        doc_id="WEB-900",
        url="https://example.com/x",
        title="Example",
        sections=_sections_with_table(),
        digest="d",
        tags=[],
        content_hash="sha256:abc",
        docs_dir=tmp_path,
        content_type="General",
        extract_tables=True,
        requested_url="https://example.com/x",
        lang="en-US",
    )
    return tmp_path


# ── 1. the resolver ─────────────────────────────────────────────────────────────
def test_resolver_accepts_tables_and_sections(tmp_path: Path) -> None:
    from mantisfetch_docreader import _resolve_manifest_section_path as resolve

    doc = tmp_path / "doc"
    (doc / "sections").mkdir(parents=True)
    (doc / "tables").mkdir()

    assert resolve(doc, "sections/01-a.md") is not None
    assert resolve(doc, "tables/table-01.md") is not None


def test_resolver_still_rejects_escapes(tmp_path: Path) -> None:
    """Widening to tables/ must not widen to anything else."""
    from mantisfetch_docreader import _resolve_manifest_section_path as resolve

    doc = tmp_path / "doc"
    (doc / "sections").mkdir(parents=True)
    (doc / "tables").mkdir()

    assert resolve(doc, "../../etc/passwd.md") is None
    assert resolve(doc, "tables/../../outside.md") is None
    assert resolve(doc, "/etc/passwd.md") is None
    assert resolve(doc, "digest.md") is None  # doc root is not a section dir
    assert resolve(doc, "tables/table-01.json") is None  # .md only
    assert resolve(doc, 42) is None


# ── 2. the readers actually reach table content ─────────────────────────────────
def test_section_records_include_tables(captured: Path) -> None:
    from mantisfetch_docreader import _load_section_records

    _, records = _load_section_records(captured, "WEB-900")
    assert len(records) == 2, "the table record was dropped"
    assert any(TABLE_ONLY_TOKEN in r["text"] for r in records)


def test_chunks_include_tables(captured: Path, client: TestClient, monkeypatch) -> None:
    import mantisfetch_common.storage as storage

    monkeypatch.setattr(storage, "DEFAULT_DOCS_DIR", captured)
    resp = client.post("/doc/library/WEB-900/chunks", json={"include_text": True})
    assert resp.status_code == 200
    body = "\n".join(c.get("text") or "" for c in resp.json()["chunks"])
    assert TABLE_ONLY_TOKEN in body


def test_search_sections_finds_table_cell(captured: Path, client: TestClient, monkeypatch) -> None:
    import mantisfetch_common.storage as storage

    monkeypatch.setattr(storage, "DEFAULT_DOCS_DIR", captured)
    resp = client.post("/doc/library/WEB-900/search_sections", json={"q": TABLE_ONLY_TOKEN})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_search_text_finds_table_cell(captured: Path, client: TestClient, monkeypatch) -> None:
    import mantisfetch_common.storage as storage

    monkeypatch.setattr(storage, "DEFAULT_DOCS_DIR", captured)
    resp = client.get("/doc/library/search_text", params={"q": TABLE_ONLY_TOKEN})
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


# ── 3. full.md ──────────────────────────────────────────────────────────────────
def test_full_md_written_and_served(captured: Path, client: TestClient, monkeypatch) -> None:
    import mantisfetch_common.storage as storage

    full_path = captured / "General" / "WEB-900" / "full.md"
    assert full_path.exists(), "full.md not written"
    text = full_path.read_text(encoding="utf-8")
    assert "Ordinary prose" in text and TABLE_ONLY_TOKEN in text

    monkeypatch.setattr(storage, "DEFAULT_DOCS_DIR", captured)
    resp = client.get("/doc/library/WEB-900/full")
    assert resp.status_code == 200
    assert TABLE_ONLY_TOKEN in resp.json()["content"]

    manifest = json.loads((captured / "General" / "WEB-900" / "manifest.json").read_text())
    assert manifest["paths"]["full"] == "full.md"


# ── 4. tables.json sidecar + fmt=json ───────────────────────────────────────────
def test_tables_sidecar_written(captured: Path) -> None:
    doc_dir = captured / "General" / "WEB-900"
    sidecar = json.loads((doc_dir / "tables.json").read_text())
    assert [e["table_id"] for e in sidecar] == ["table-01"]
    assert sidecar[0]["json_file"] == "tables/table-01.json"

    manifest = json.loads((doc_dir / "manifest.json").read_text())
    assert manifest["tables"] == sidecar
    assert manifest["paths"]["tables"] == "tables.json"


def test_table_json_endpoint(captured: Path, client: TestClient, monkeypatch) -> None:
    import mantisfetch_common.storage as storage

    monkeypatch.setattr(storage, "DEFAULT_DOCS_DIR", captured)
    resp = client.get("/doc/library/WEB-900/table/table-01/json")
    assert resp.status_code == 200
    table = resp.json()["table"]
    assert table["header"] == ["Name", "Count"]
    assert [c for r in table["rows"] for c in r["cells"]][0] == TABLE_ONLY_TOKEN
    assert table["stored_row_count"] == 2


def test_table_json_absent_without_tables(tmp_path: Path) -> None:
    """A capture with no tables writes no sidecar and no empty tables/ dir."""
    import mantisfetch_browser as lb

    lb._persist_web_capture(
        doc_id="WEB-901", url="https://example.com/y", title="Y",
        sections=[{"sid": "s_1", "h": "H", "t": "body", "type": "text"}],
        digest="d", tags=[], content_hash="sha256:def", docs_dir=tmp_path,
        content_type="General", extract_tables=False,
        requested_url="https://example.com/y", lang="en-US",
    )
    doc_dir = tmp_path / "General" / "WEB-901"
    assert not (doc_dir / "tables.json").exists()
    manifest = json.loads((doc_dir / "manifest.json").read_text())
    assert manifest["tables"] == []
    assert "tables" not in manifest["paths"]


# ── 5. markdown → cells round-trip ──────────────────────────────────────────────
def test_web_table_rows_parses_header_and_cells() -> None:
    from mantisfetch_browser import _web_table_rows

    header, rows = _web_table_rows(
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
    )
    assert header == ["A", "B"]
    assert rows == [["1", "2"], ["3", "4"]]


def test_web_table_rows_without_header() -> None:
    from mantisfetch_browser import _web_table_rows

    header, rows = _web_table_rows("| 1 | 2 |\n| 3 | 4 |")
    assert header == []
    assert rows == [["1", "2"], ["3", "4"]]


def test_web_table_rows_keeps_escaped_pipes() -> None:
    """The extractor rewrites in-cell '|' to '¦', so splitting on '|' is lossless."""
    from mantisfetch_browser import _web_table_rows

    header, rows = _web_table_rows("| A | B |\n| --- | --- |\n| x ¦ y | z |")
    assert rows == [["x ¦ y", "z"]]
    assert header == ["A", "B"]


def test_web_table_rows_ignores_non_table_lines() -> None:
    from mantisfetch_browser import _web_table_rows

    header, rows = _web_table_rows(
        "# Heading\n\n| A |\n| --- |\n| 1 |\n\n[... 223 rows total, showing first 80 ...]"
    )
    assert header == ["A"]
    assert rows == [["1"]]


# ── 6. the MCP surface ──────────────────────────────────────────────────────────
def test_mcp_exposes_doc_search_text() -> None:
    """/library/search_text existed server-side but had no MCP tool, so an agent
    could only ever search metadata."""
    import asyncio

    import mantisfetch_mcp as mm

    names = {t.name for t in asyncio.run(mm.mcp.list_tools())}
    assert "doc_search_text" in names


def test_mcp_doc_search_text_calls_the_right_endpoint(monkeypatch) -> None:
    import asyncio

    import mantisfetch_mcp as mm

    seen: dict = {}

    async def fake_get(path: str, params: dict | None = None):
        seen["path"] = path
        seen["params"] = params
        return {"results": [], "total": 0}

    monkeypatch.setattr(mm, "_doc_get", fake_get)
    asyncio.run(mm.doc_search_text(q="cavitation", doc_id="WEB-001", scope="section"))

    assert seen["path"] == "/library/search_text"
    assert seen["params"]["q"] == "cavitation"
    assert seen["params"]["doc_id"] == "WEB-001"
    assert seen["params"]["scope"] == "section"

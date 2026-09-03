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


def test_search_text_scope_full_matches_a_capture(
    captured: Path, client: TestClient, monkeypatch
) -> None:
    """Without full.md this scope could never match a capture at all."""
    import mantisfetch_common.storage as storage

    monkeypatch.setattr(storage, "DEFAULT_DOCS_DIR", captured)
    resp = client.get(
        "/doc/library/search_text", params={"q": TABLE_ONLY_TOKEN, "scope": "full"}
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_truncated_table_reports_dom_rows_and_stored_rows(tmp_path: Path) -> None:
    """A truncated table's row_count is the DOM total (header included) while
    stored_row_count counts the data rows on disk — the GDP case, where the DOM
    had 223 rows and only a fraction reached the file."""
    import mantisfetch_browser as lb

    body = "| Country | GDP |\n| --- | --- |\n" + "\n".join(
        f"| C{n} | {n} |" for n in range(1, 6)
    )
    lb._persist_web_capture(
        doc_id="WEB-902", url="https://example.com/gdp", title="GDP",
        sections=[{
            "sid": "t_1", "h": "[Table] GDP", "t": body, "type": "table",
            "table_meta": {"rows": 223, "cols": 2, "has_header": True, "truncated": True},
        }],
        digest="d", tags=[], content_hash="sha256:gdp", docs_dir=tmp_path,
        content_type="General", extract_tables=True,
        requested_url="https://example.com/gdp", lang="en-US",
    )
    payload = json.loads(
        (tmp_path / "General" / "WEB-902" / "tables" / "table-01.json").read_text()
    )
    assert payload["row_count"] == 223  # what the DOM had
    assert payload["stored_row_count"] == 5  # what actually reached disk
    assert payload["truncated"] is True
    assert payload["header"] == ["Country", "GDP"]


def test_row_count_fallback_counts_the_header_too(tmp_path: Path) -> None:
    """Without table_meta both counts must still use their own definitions:
    row_count includes the header row, stored_row_count does not."""
    import mantisfetch_browser as lb

    lb._persist_web_capture(
        doc_id="WEB-903", url="https://example.com/t", title="T",
        sections=[{
            "sid": "t_1", "h": "[Table] X", "type": "table",
            "t": "| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |",
        }],
        digest="d", tags=[], content_hash="sha256:t", docs_dir=tmp_path,
        content_type="General", extract_tables=True,
        requested_url="https://example.com/t", lang="en-US",
    )
    doc_dir = tmp_path / "General" / "WEB-903"
    payload = json.loads((doc_dir / "tables" / "table-01.json").read_text())
    assert payload["row_count"] == 3  # 2 data rows + the header
    assert payload["stored_row_count"] == 2
    assert payload["header_rows"] == 1

    # the sidecar entry keeps docreader's shape so consumers need no special case
    entry = json.loads((doc_dir / "tables.json").read_text())[0]
    assert entry["continued_from"] is None and entry["continued_to"] is None
    assert entry["row_count"] == 3 and entry["header_rows"] == 1


# ── 4b. the upload retry path must not swallow a capture ────────────────────────
def test_retry_summary_refuses_a_web_capture(
    captured: Path, client: TestClient, monkeypatch
) -> None:
    """POST /library/{id}/summary reconstructs a document and rewrites it via
    write_output_extract_only(source="upload"). Run over a web capture that would
    rewrite full.md, copy the capture's table markdown into sections/, and relabel
    the document as an upload.

    Before the resolver accepted tables/, a capture *with* tables happened to fail
    loudly here ("section file missing"); one *without* tables was already being
    corrupted silently. Captures own their own deferred-summary path, so this now
    refuses outright.
    """
    import mantisfetch_common.storage as storage

    monkeypatch.setattr(storage, "DEFAULT_DOCS_DIR", captured)
    doc_dir = captured / "General" / "WEB-900"
    before = json.loads((doc_dir / "manifest.json").read_text())
    full_before = (doc_dir / "full.md").read_text(encoding="utf-8")

    resp = client.post("/doc/library/WEB-900/summary")
    assert resp.status_code == 409
    assert "web capture" in resp.json()["detail"]

    after = json.loads((doc_dir / "manifest.json").read_text())
    assert after["source"] == before["source"] == "web_capture"
    assert after["sections"] == before["sections"]
    assert (doc_dir / "full.md").read_text(encoding="utf-8") == full_before
    # the table stayed in tables/, it was not copied into sections/
    assert sorted(p.name for p in (doc_dir / "sections").iterdir()) == [
        "01-s_0001-Intro.md"
    ]


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


def test_mcp_doc_search_text_passes_tags_through(monkeypatch) -> None:
    import asyncio

    import mantisfetch_mcp as mm

    seen: dict = {}

    async def fake_get(path: str, params: dict | None = None):
        seen.update(params or {})
        return {"results": [], "total": 0}

    monkeypatch.setattr(mm, "_doc_get", fake_get)
    asyncio.run(mm.doc_search_text(q="x", tags="bid,2026"))
    assert seen["tags"] == "bid,2026"

    seen.clear()
    asyncio.run(mm.doc_search_text(q="x"))
    assert "tags" not in seen  # omitted rather than sent empty
    assert "doc_id" not in seen


# ── a library-wide scan degrades rather than failing ────────────────────────────
def test_one_unreadable_document_does_not_fail_the_whole_search(
    captured: Path, client: TestClient, monkeypatch
) -> None:
    """search_text walks every document in the library and reads files off disk.
    An undecodable section, a permission error, or a manifest whose shape
    surprises the response model turned the whole query into a 500 — no partial
    results, and nothing saying which document caused it.

    The point is that the healthy document still answers, so the failure has to
    be aimed at one document: blowing up the read globally would prove only that
    the endpoint returns 200, which an empty healthy library also does.

    A v1.7.0 retest reported that 500. It could not be reproduced against a real
    55-document library, a 2 MB capture, LANG=zh, or a library mixing pre- and
    post-upgrade captures, so this hardens the class of failure rather than a
    known instance.
    """
    import mantisfetch_docreader as dr

    import mantisfetch_common.storage as storage

    monkeypatch.setattr(storage, "DEFAULT_DOCS_DIR", captured)

    # a second, healthy document carrying the same term
    lb_persist_healthy(captured, "WEB-902", TABLE_ONLY_TOKEN)

    real_snippet = dr._make_snippet

    def snippet_that_breaks_on_one_doc(text: str, query: str, radius: int = 90) -> str:
        if "poisoned" in text:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "simulated bad bytes")
        return real_snippet(text, query, radius)

    monkeypatch.setattr(dr, "_make_snippet", snippet_that_breaks_on_one_doc)
    lb_persist_healthy(captured, "WEB-903", f"poisoned {TABLE_ONLY_TOKEN}")

    resp = client.get("/doc/library/search_text", params={"q": TABLE_ONLY_TOKEN})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["skipped"] == 1, "the broken document should be counted once"
    found = {r["doc_id"] for r in body["results"]}
    assert "WEB-902" in found, "the healthy document must still answer"
    assert "WEB-903" not in found, "the broken one contributed nothing"


def lb_persist_healthy(docs_dir: Path, doc_id: str, body: str) -> None:
    """A minimal capture carrying ``body`` in its only section."""
    import mantisfetch_browser as lb

    lb._persist_web_capture(
        doc_id=doc_id, url=f"https://example.com/{doc_id}", title=doc_id,
        sections=[{"sid": f"s_{doc_id}", "h": "H", "t": body, "type": "text"}],
        digest="d", tags=[], content_hash=f"sha256:{doc_id}", docs_dir=docs_dir,
        content_type="General", extract_tables=False,
        requested_url=f"https://example.com/{doc_id}", lang="en-US",
    )


def test_skipped_is_zero_on_a_healthy_library(
    captured: Path, client: TestClient, monkeypatch
) -> None:
    import mantisfetch_common.storage as storage

    monkeypatch.setattr(storage, "DEFAULT_DOCS_DIR", captured)
    body = client.get("/doc/library/search_text", params={"q": TABLE_ONLY_TOKEN}).json()
    assert body["skipped"] == 0
    assert body["total"] >= 1


# ── null section titles ─────────────────────────────────────────────────────────
# A v1.7.1 retest supplied the log line the per-document guard was added to
# produce:
#
#   search_text degraded on WEB-003: AttributeError: 'NoneType' object has no
#   attribute 'lower'
#
# The cause is dict.get(key, default) returning the *stored* None when the key is
# present and null, rather than the default. Capture wrote "title": null for any
# section without a heading, and search_text then called .lower() on it — one
# such document turned a whole-library search into a 500 for every other.
def _seed_manifest_with_null_title(docs_dir: Path, doc_id: str, body: str) -> None:
    """A capture as written before the fix: a section whose title is null."""
    doc = docs_dir / "General" / doc_id
    (doc / "sections").mkdir(parents=True, exist_ok=True)
    (doc / "sections" / "01-s_x-section.md").write_text(body, encoding="utf-8")
    (doc / "manifest.json").write_text(json.dumps({
        "doc_id": doc_id, "filename": doc_id, "file_type": "web_capture",
        "source": "web_capture", "content_type": "General",
        "storage_path": f"General/{doc_id}",
        "paths": {"digest": "digest.md", "sections_dir": "sections/"},
        "sections": [{"sid": "s_x", "index": 1, "title": None,
                      "char_count": len(body), "type": "text",
                      "file": "sections/01-s_x-section.md"}],
        "provenance": {"source": "web_capture", "source_url": f"https://e.com/{doc_id}"},
    }), encoding="utf-8")
    # Through the index store, not doc-index.json: _load_doc_index prefers the
    # store and only falls back to the JSON when the store is empty, so an entry
    # written straight to the file is invisible to every reader.
    from mantisfetch_common import doc_index_store as dis

    dis.upsert_document(docs_dir, {
        "id": doc_id, "filename": doc_id, "file_type": "web_capture",
        "content_type": "General", "storage_path": f"General/{doc_id}",
        "source": "web_capture", "source_url": f"https://e.com/{doc_id}",
        "created_at": "2026-08-01T00:00:00Z",
    })


def test_a_null_section_title_is_searchable_not_skipped(
    captured: Path, client: TestClient, monkeypatch
) -> None:
    import mantisfetch_common.storage as storage

    monkeypatch.setattr(storage, "DEFAULT_DOCS_DIR", captured)
    _seed_manifest_with_null_title(captured, "WEB-904", "body mentioning Zylobactor once")

    resp = client.get("/doc/library/search_text", params={"q": "Zylobactor", "scope": "section"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # the guard would have caught the crash and reported it as skipped; the
    # point of the fix is that the document is *searched*, not merely survived
    assert body["skipped"] == 0
    assert "WEB-904" in {r["doc_id"] for r in body["results"]}


def test_capture_never_writes_a_null_section_title(tmp_path: Path) -> None:
    """The other half: stop producing the data that needed defending against."""
    import mantisfetch_browser as lb

    lb._persist_web_capture(
        doc_id="WEB-905", url="https://example.com/n", title="N",
        sections=[
            {"sid": "s_1", "h": None, "t": "prose with no heading at all", "type": "text"},
            {"sid": "t_1", "h": None, "t": "| A |\n| --- |\n| 1 |", "type": "table",
             "table_meta": {"rows": 1, "cols": 1}},
        ],
        digest="d", tags=[], content_hash="sha256:n", docs_dir=tmp_path,
        content_type="General", extract_tables=True,
        requested_url="https://example.com/n", lang="en-US",
    )
    manifest = json.loads(
        (tmp_path / "General" / "WEB-905" / "manifest.json").read_text(encoding="utf-8")
    )
    for section in manifest["sections"]:
        assert section["title"] is not None, section
        assert isinstance(section["title"], str)

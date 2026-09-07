"""Four things a real agent hit on v1.8.1 that the API was telling it wrongly.

From an MCP-first evaluation run against the deployed build. None of these
stopped the work; each made the service say something that was not true, and an
agent acts on what it is told:

  - an empty file uploaded over MCP came back as "you gave me no source";
  - a document ingested with generate_summary=false had a brief that said a
    summary was pending, while its manifest said `disabled`;
  - a metadata search for a word that only appears in a body returned a bare
    zero, which reads as "not in the library";
  - `/web/capture` was the one document-producing response with no `kind`.
"""

import pytest


def test_an_empty_upload_over_mcp_is_reported_as_an_empty_file(monkeypatch, tmp_path) -> None:
    """`if s` treats an empty base64 string as an absent argument, so the caller
    was told to check its arguments rather than its file."""
    import mantisfetch_mcp as mm

    import mantisfetch_common.storage as cs

    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", docs)

    import asyncio

    with pytest.raises(Exception) as caught:
        asyncio.run(mm.doc_parse(content_b64="", filename="empty.txt"))

    message = str(caught.value)
    assert "provide exactly one of" not in message, message
    assert "empty" in message.lower(), message


def test_a_disabled_summary_does_not_say_it_is_pending(client, tmp_path, monkeypatch) -> None:
    """generate_summary=false asks for no summary, so nothing is coming. The
    manifest already recorded `disabled`; the brief said the opposite."""
    import mantisfetch_common.storage as cs

    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", docs)

    resp = client.post(
        "/doc/parse",
        files={"file": ("p.html", b"<h1>T</h1><p>Body worth keeping.</p>", "text/html")},
        data={"summary_mode": "off", "generate_summary": "false"},
    )
    doc_id = resp.json()["doc_id"]

    brief = client.get(f"/doc/library/{doc_id}/brief").json()["content"]
    assert "pending" not in brief.lower()
    assert "待生成" not in brief
    status = client.get(f"/doc/library/{doc_id}/summary").json()["summary"].get("status")
    assert status == "disabled"


def test_an_empty_metadata_search_says_where_the_body_search_is(
    client, tmp_path, monkeypatch
) -> None:
    """A term that appears only in the text returns zero from /library/search,
    which reads exactly like "not in the library" — and an agent that stops
    there is wrong."""
    import mantisfetch_common.storage as cs

    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", docs)

    client.post(
        "/doc/parse",
        files={
            "file": ("p.html", b"<h1>Title</h1><p>cavitation bubbles collapse.</p>", "text/html")
        },
        data={"summary_mode": "off", "generate_summary": "false"},
    )

    meta = client.get("/doc/library/search", params={"q": "cavitation"}).json()
    assert meta["total"] == 0
    assert "search_text" in (meta.get("hint") or ""), meta.get("hint")

    body = client.get("/doc/library/search_text", params={"q": "cavitation"}).json()
    assert body["total"] >= 1
    assert body.get("hint") is None, "a search that found something needs no hint"


def test_a_search_that_finds_something_carries_no_hint(client, tmp_path, monkeypatch) -> None:
    import mantisfetch_common.storage as cs

    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", docs)

    client.post(
        "/doc/parse",
        files={"file": ("findme.html", b"<h1>T</h1><p>Body worth keeping.</p>", "text/html")},
        data={"summary_mode": "off", "generate_summary": "false"},
    )
    hits = client.get("/doc/library/search", params={"q": "findme"}).json()

    assert hits["total"] >= 1
    assert hits.get("hint") is None


def test_a_capture_response_carries_kind_like_every_other_one(tmp_path, monkeypatch) -> None:
    """The release notes say every face that identifies a document carries
    `kind`. /web/capture was the exception."""
    from mantisfetch_browser.models import CaptureResponse

    resp = CaptureResponse(doc_id="WEB-1", digest="d", section_count=1, table_count=0)
    assert resp.kind == "parsed"

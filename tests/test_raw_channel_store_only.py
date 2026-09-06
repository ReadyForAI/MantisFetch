"""The raw channel: store the original, run no parser (SharedSpecs 20260708 amd-1).

Markdown and images are the two things a chat carries that MantisFetch has no
parser for. Before this they were 422s at the ingest edge, which showed up in
AULO's UI as "解析失败" on a file the user had every reason to expect would work.

Three properties are load-bearing for other products and are tested here rather
than described:

  - a raw ingest does not queue behind parses (AULO's ingest model is "land the
    bytes and answer"; #251 made every /parse wait for a slot);
  - the two allow-lists never overlap (HarnessServer picks between its `stored`
    and `ok` terminal states on that);
  - `kind` is on the response, not only in the manifest — the callers that have
    to branch on it read the response.
"""

import asyncio
import json

import pytest
from starlette.testclient import TestClient

MD = b"# Title\n\nBody line one.\nBody line two.\n"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _store(client: TestClient, content=MD, name="notes.md", **extra):
    return client.post(
        "/doc/parse",
        files={"file": (name, content, "application/octet-stream")},
        data={"store_only": "true", **extra},
    )


@pytest.fixture()
def docs_dir(monkeypatch, tmp_path):
    import mantisfetch_common.storage as cs

    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


# ── what the two channels are ────────────────────────────────────────────────────
def test_the_two_allow_lists_never_overlap() -> None:
    """HarnessServer's `stored` vs `ok` decision rests on this, and both sets are
    ours to change — so it is checked at import, and asserted here so the reason
    is written down where someone editing the list will read it."""
    import mantisfetch_docreader as dr

    assert dr.RAW_EXTENSIONS & dr.SUPPORTED_EXTENSIONS == set()


def test_markdown_is_stored_not_parsed(client, docs_dir) -> None:
    resp = _store(client, name="notes.md")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "raw"
    assert body["section_count"] == 0 and body["total_pages"] == 0
    assert body["source_ref"] == "source/notes.md"

    manifest = client.get(f"/doc/library/{body['doc_id']}/manifest").json()
    assert manifest["kind"] == "raw"
    assert manifest["media_type"] == "text/markdown"
    # Only what exists: pointing at digest/brief/full would send every reader to
    # four files that were never written.
    assert manifest["paths"] == {"source": "source/notes.md"}
    assert manifest["sections"] == []


def test_a_parsed_document_says_so_on_the_response(client, docs_dir) -> None:
    """`kind` is never absent and never null on the way out — the callers that
    branch on it read the response, not the manifest."""
    resp = client.post(
        "/doc/parse",
        files={"file": ("page.html", b"<h1>T</h1><p>Body worth keeping.</p>", "text/html")},
        data={"summary_mode": "off", "generate_summary": "false"},
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "parsed"


def test_an_image_is_stored_whole(client, docs_dir) -> None:
    body = _store(client, content=PNG, name="shot.png").json()
    assert body["kind"] == "raw"

    raw = client.get(f"/doc/library/{body['doc_id']}/source")
    assert raw.status_code == 200
    assert raw.content == PNG
    assert raw.headers["content-type"].startswith("image/png")


# ── the refusals ─────────────────────────────────────────────────────────────────
def test_a_parseable_format_may_not_take_the_raw_channel(client, docs_dir) -> None:
    """store_only is a parameter, the extension is a fact, and they can disagree.
    Storing a .pdf raw would break the rule that the extension alone decides the
    channel."""
    resp = _store(client, content=b"%PDF-1.4 x", name="report.pdf")

    assert resp.status_code == 422
    assert "drop store_only" in resp.json()["detail"]


def test_an_unknown_format_is_refused_by_both_channels(client, docs_dir) -> None:
    assert _store(client, content=b"MZ", name="tool.exe").status_code == 422


def test_store_only_needs_somewhere_to_put_the_original(client, docs_dir, monkeypatch) -> None:
    """With source files off the request would otherwise succeed and store
    nothing: a document with neither parse products nor an original."""
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "STORE_SOURCE_FILES", False)
    resp = _store(client)

    assert resp.status_code == 422
    assert "MANTISFETCH_STORE_SOURCE_FILES" in resp.json()["detail"]


def test_the_per_type_ceiling_bounds_the_upload(client, docs_dir, monkeypatch) -> None:
    """The ceiling is a contract AULO pre-checks against, so it has to stop the
    stream rather than be checked after the bytes have landed."""
    monkeypatch.setenv("MANTISFETCH_RAW_MAX_MD_MB", "1")
    resp = _store(client, content=b"#" * (1024 * 1024 + 10))

    assert resp.status_code == 413
    assert str(1024 * 1024) in resp.json()["detail"]


def test_markdown_and_images_have_separate_ceilings() -> None:
    """2 MiB of markdown is ~500k tokens of context; 8 MiB is one image on one
    turn. The numbers are pinned because AULO refuses against the same two."""
    import mantisfetch_docreader as dr

    assert dr._raw_max_bytes(".md") == 2_097_152
    assert dr._raw_max_bytes(".png") == 8_388_608


def test_an_empty_file_is_still_nothing(client, docs_dir) -> None:
    assert _store(client, content=b"", name="empty.md").status_code == 422


# ── the property AULO's ingest model rests on ────────────────────────────────────
def test_a_raw_ingest_does_not_wait_for_a_parse_slot(client, docs_dir, monkeypatch) -> None:
    """The interaction with #251: every /parse now waits for a slot, up to the
    caller's budget or ten minutes. A raw file has nothing to parse, and an
    8 MiB image queued behind two OCR jobs would break "land the bytes and
    answer" (IRP 20260801 §2.2).

    A plain Semaphore(0) — no slot will ever free, so passing means the request
    never asked for one.
    """
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "_parse_sem", asyncio.Semaphore(0))
    monkeypatch.setenv("MANTISFETCH_PARSE_QUEUE_MAX_WAIT_SEC", "0.3")

    assert _store(client).status_code == 200


def test_a_parse_still_waits_on_the_same_gate(client, docs_dir, monkeypatch) -> None:
    """The other half: the bypass is for store_only, not for everyone."""
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "_parse_sem", asyncio.Semaphore(0))
    monkeypatch.setenv("MANTISFETCH_PARSE_QUEUE_MAX_WAIT_SEC", "0.3")

    resp = client.post(
        "/doc/parse",
        files={"file": ("page.html", b"<h1>T</h1><p>Body.</p>", "text/html")},
        data={"summary_mode": "off", "generate_summary": "false"},
    )
    assert resp.status_code == 429


# ── duplicate content is reported, never merged ──────────────────────────────────
def test_the_same_bytes_twice_are_reported_not_merged(client, docs_dir) -> None:
    """Merging would break `response.doc_id == request.doc_id` and, worse, let
    one chat's cleanup delete another chat's attachment — this library has no
    reference counting."""
    first = _store(client, name="a.md").json()
    second = _store(client, name="b.md").json()

    assert first["dedup"] == "miss" and first["existing_doc_id"] is None
    assert second["dedup"] == "hit"
    assert second["existing_doc_id"] == first["doc_id"]
    assert second["doc_id"] != first["doc_id"]
    # Both documents are real and independently readable.
    for doc_id in (first["doc_id"], second["doc_id"]):
        assert client.get(f"/doc/library/{doc_id}/source").status_code == 200


def test_deleting_one_copy_leaves_the_other(client, docs_dir) -> None:
    """The reason detection stops short of merging, stated as a test."""
    first = _store(client, name="a.md").json()
    second = _store(client, name="b.md").json()

    assert client.delete(f"/doc/library/{first['doc_id']}").json()["deleted"] is True
    assert client.get(f"/doc/library/{second['doc_id']}/source").status_code == 200


def test_the_parse_channel_does_not_look_for_duplicates(client, docs_dir) -> None:
    """Same bytes, different parse options, different document — a content hit
    there would silently turn "re-parse with more OCR pages" into a no-op."""
    html = b"<h1>T</h1><p>Body worth keeping.</p>"
    data = {"summary_mode": "off", "generate_summary": "false"}
    first = client.post("/doc/parse", files={"file": ("p.html", html, "text/html")}, data=data)
    second = client.post("/doc/parse", files={"file": ("p.html", html, "text/html")}, data=data)

    assert second.json()["dedup"] == "miss"
    assert second.json()["existing_doc_id"] is None
    assert second.json()["doc_id"] != first.json()["doc_id"]


# ── the read faces ───────────────────────────────────────────────────────────────
def test_source_info_answers_without_bytes(client, docs_dir) -> None:
    """The shape an agent can afford to hold: enough to decide, no payload."""
    doc_id = _store(client, content=PNG, name="shot.png").json()["doc_id"]

    info = client.get(f"/doc/library/{doc_id}/source/info").json()
    assert info == {
        "doc_id": doc_id,
        "filename": "shot.png",
        "media_type": "image/png",
        "size_bytes": len(PNG),
        "kind": "raw",
    }


def test_a_text_window_pages_by_line(client, docs_dir) -> None:
    doc_id = _store(client, content=b"".join(f"line {i}\n".encode() for i in range(10))).json()[
        "doc_id"
    ]

    first = client.get(f"/doc/library/{doc_id}/source/info", params={"limit": 4}).json()
    assert first["text"] == "line 0\nline 1\nline 2\nline 3\n"
    assert first["offset"] == 0 and first["next_offset"] == 4
    assert first["total_lines"] == 10 and first["truncated"] is False

    last = client.get(f"/doc/library/{doc_id}/source/info", params={"offset": 8, "limit": 4}).json()
    assert last["text"] == "line 8\nline 9\n"
    assert last["next_offset"] is None


def test_a_window_is_capped_in_bytes_not_only_lines(client, docs_dir) -> None:
    """Lines are not a length. Asking for two 40 KiB lines must not return 80 KiB
    just because the line count was small."""
    import mantisfetch_docreader as dr

    body = ("x" * 40000 + "\n").encode() * 2
    doc_id = _store(client, content=body).json()["doc_id"]

    win = client.get(f"/doc/library/{doc_id}/source/info", params={"limit": 2}).json()
    assert len(win["text"].encode()) <= dr.RAW_TEXT_WINDOW_MAX_BYTES
    assert win["truncated"] is True
    assert win["next_offset"] == 1


def test_one_over_long_line_is_cut_rather_than_returned_empty(client, docs_dir) -> None:
    """A paging caller must never be handed nothing and told to continue."""
    import mantisfetch_docreader as dr

    doc_id = _store(client, content=b"y" * (dr.RAW_TEXT_WINDOW_MAX_BYTES * 2)).json()["doc_id"]

    win = client.get(f"/doc/library/{doc_id}/source/info", params={"limit": 1}).json()
    assert len(win["text"]) == dr.RAW_TEXT_WINDOW_MAX_BYTES
    assert win["truncated"] is True
    assert win["next_offset"] is None


def test_an_image_has_no_text_window(client, docs_dir) -> None:
    """Almost always the wrong doc_id, so it says so instead of answering with
    metadata as if the window had been ignored."""
    doc_id = _store(client, content=PNG, name="shot.png").json()["doc_id"]

    resp = client.get(f"/doc/library/{doc_id}/source/info", params={"offset": 0})
    assert resp.status_code == 422
    assert "no text window" in resp.json()["detail"]


def test_a_document_with_no_stored_original_says_which_setting(
    client, docs_dir, monkeypatch
) -> None:
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "STORE_SOURCE_FILES", False)
    doc_id = client.post(
        "/doc/parse",
        files={"file": ("page.html", b"<h1>T</h1><p>Body.</p>", "text/html")},
        data={"summary_mode": "off", "generate_summary": "false"},
    ).json()["doc_id"]

    resp = client.get(f"/doc/library/{doc_id}/source")
    assert resp.status_code == 404
    assert "MANTISFETCH_STORE_SOURCE_FILES" in resp.json()["detail"]


# ── what a raw document does to the rest of the library ──────────────────────────
def test_the_three_tier_readers_404_rather_than_break(client, docs_dir) -> None:
    doc_id = _store(client).json()["doc_id"]

    for tier in ("digest", "brief", "full"):
        assert client.get(f"/doc/library/{doc_id}/{tier}").status_code == 404


def test_listing_sections_answers_with_the_kind_rather_than_a_bare_empty_list(
    client, docs_dir
) -> None:
    """A listing endpoint should answer for a document that exists. "No
    sections" is the true answer here — what an agent needs alongside it is why,
    or an empty list reads as "this document came out empty"."""
    doc_id = _store(client).json()["doc_id"]

    body = client.get(f"/doc/library/{doc_id}/sections").json()
    assert body["kind"] == "raw"
    assert body["sections"] == []


def test_a_raw_document_cannot_be_summarized(client, docs_dir) -> None:
    """The reachable agent path: digest 404s, so ask for a summary. That handler
    reconstructs a parsed document from storage and rewrites it — over a raw
    document it would write digest/brief/full and re-index it without its kind.
    The web-capture branch next to it refuses the same class of rewrite."""
    doc_id = _store(client).json()["doc_id"]

    status = client.get(f"/doc/library/{doc_id}/summary").json()
    assert status["kind"] == "raw"  # says so before the caller reaches for POST

    resp = client.post(f"/doc/library/{doc_id}/summary")
    assert resp.status_code == 409
    assert "stored, not parsed" in resp.json()["detail"]
    # Still what it was.
    assert client.get(f"/doc/library/{doc_id}/manifest").json()["kind"] == "raw"
    assert client.get(f"/doc/library/{doc_id}/full").status_code == 404


def test_health_advertises_the_raw_formats_separately(client) -> None:
    """The skill tells clients to check health before uploading. Without this a
    client sees .md missing from supported_formats and never tries store_only —
    and merging the two lists would tell HarnessServer they get parsed."""
    body = client.get("/doc/health").json()

    assert "md" in body["raw_formats"] and "png" in body["raw_formats"]
    assert "md" not in body["supported_formats"]


def test_full_text_search_skips_raw_documents(client, docs_dir) -> None:
    """Recorded rather than fixed: full-text search reads full.md and sections,
    and a raw document has neither. Making it searchable would mean parsing it.
    The point of the test is that the scan does not fail on one."""
    _store(client, content=b"# Quarterly plan\n\nunmistakable-token\n")
    client.post(
        "/doc/parse",
        files={"file": ("p.html", b"<h1>T</h1><p>unmistakable-token here.</p>", "text/html")},
        data={"summary_mode": "off", "generate_summary": "false"},
    )

    hits = client.get("/doc/library/search_text", params={"q": "unmistakable-token"}).json()
    # One document, matched once per scope (full + section) — and it is the
    # parsed one; the markdown holding the same token is not reachable here.
    assert {r["file_type"] for r in hits["results"]} == {"html"}


def test_delete_takes_the_original_with_it(client, docs_dir) -> None:
    """M4 is "zero changes" — the proof is that the existing rmtree already
    covers source/."""
    doc_id = _store(client).json()["doc_id"]
    storage_path = json.loads((docs_dir / "doc-index.json").read_text())["documents"][0][
        "storage_path"
    ]

    assert client.delete(f"/doc/library/{doc_id}").json()["deleted"] is True
    assert not (docs_dir / storage_path).exists()


def test_replacing_a_parsed_document_leaves_no_parse_products(client, docs_dir) -> None:
    """A raw document is its original file and nothing else. Leaving the old
    sections behind would make it read as parsed while claiming kind=raw."""
    parsed = client.post(
        "/doc/parse",
        files={"file": ("p.html", b"<h1>T</h1><p>Body worth keeping.</p>", "text/html")},
        data={"summary_mode": "off", "generate_summary": "false", "doc_id": "DOC-901"},
    )
    assert parsed.status_code == 200

    resp = _store(client, doc_id="DOC-901", replace="true")
    assert resp.status_code == 200
    assert resp.json()["dedup"] == "replaced"

    manifest = client.get("/doc/library/DOC-901/manifest").json()
    assert manifest["kind"] == "raw"
    assert client.get("/doc/library/DOC-901/full").status_code == 404
    # The library-wide FTS row is part of the replaced document's state too.
    # full.md and the lowercase caches are gone, so file-based search stops
    # matching; the row would go on answering with the old body.
    from mantisfetch_common.doc_index_store import read_fts

    assert not read_fts(docs_dir, "DOC-901")


def test_an_explicit_doc_id_that_exists_is_still_a_conflict(client, docs_dir) -> None:
    assert _store(client, doc_id="DOC-902").status_code == 200
    resp = _store(client, doc_id="DOC-902")
    assert resp.status_code == 409


def test_the_conflict_says_what_is_already_there(client, docs_dir) -> None:
    """A 409 here is first-writer-wins, and the caller has to record which
    terminal state the document reached — Harness reads that off `kind`. Without
    it a retry of a stored image is recorded as parsed, and AULO only forwards
    attachments it believes were stored, so the image never reaches the model.

    The occupant's kind, not the request's: storing onto an id that holds a
    parsed document is a parsed document that is already there.
    """
    assert _store(client, doc_id="DOC-903").status_code == 200
    raw_conflict = _store(client, doc_id="DOC-903").json()["detail"]
    assert raw_conflict["kind"] == "raw"
    assert raw_conflict["doc_id"] == "DOC-903"
    assert "doc_source" in raw_conflict["message"]

    client.post(
        "/doc/parse",
        files={"file": ("p.html", b"<h1>T</h1><p>Body worth keeping.</p>", "text/html")},
        data={"summary_mode": "off", "generate_summary": "false", "doc_id": "DOC-904"},
    )
    assert _store(client, doc_id="DOC-904").json()["detail"]["kind"] == "parsed"


def test_the_parse_channel_conflict_is_unchanged(client, docs_dir) -> None:
    """Its 409 text is what HTTP callers read today; the raw channel is new
    enough to have no such reader, which is why only that one changed shape."""
    data = {"summary_mode": "off", "generate_summary": "false", "doc_id": "DOC-905"}
    html = {"file": ("p.html", b"<h1>T</h1><p>Body worth keeping.</p>", "text/html")}
    assert client.post("/doc/parse", files=html, data=data).status_code == 200

    detail = client.post("/doc/parse", files=html, data=data).json()["detail"]
    assert isinstance(detail, str) and "already exists" in detail


# ── a failed store must not take a readable document with it ─────────────────────
def test_a_failed_replace_leaves_the_old_document_readable(client, docs_dir, monkeypatch) -> None:
    """The raw channel replaces in place, so it runs under the same rollback the
    parse path does (#212). Without it a full disk mid-copy would destroy a
    document that was perfectly readable a moment ago — and leave the index
    pointing at it."""
    import mantisfetch_docreader as dr

    parsed = client.post(
        "/doc/parse",
        files={"file": ("p.html", b"<h1>T</h1><p>Body worth keeping.</p>", "text/html")},
        data={"summary_mode": "off", "generate_summary": "false", "doc_id": "DOC-910"},
    )
    assert parsed.status_code == 200
    before = client.get("/doc/library/DOC-910/full").text

    def _boom(*a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(dr, "_persist_source_file", _boom)
    resp = _store(client, doc_id="DOC-910", replace="true")
    assert resp.status_code == 500

    # Still the document it was: full text, manifest and source all intact.
    assert client.get("/doc/library/DOC-910/full").text == before
    assert client.get("/doc/library/DOC-910/manifest").json().get("kind", "parsed") == "parsed"
    assert client.get("/doc/library/DOC-910/source").content.startswith(b"<h1>T</h1>")
    # A failed replacement is not that document's fault; marking it would report
    # a readable document as broken.
    assert not (docs_dir / "General" / "DOC-910" / dr.PARSE_FAILURE_MARKER).exists()


def test_a_failed_new_store_leaves_a_record_not_an_empty_directory(
    client, docs_dir, monkeypatch
) -> None:
    """An empty directory is indistinguishable from one mid-ingest. IRP 20260801
    §3.6 rules that a failure is recorded rather than deleted, so a caller that
    timed out can tell "it failed" from "still running"."""
    import mantisfetch_docreader as dr

    def _boom(*a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(dr, "_persist_source_file", _boom)
    assert _store(client, doc_id="DOC-911").status_code == 500

    marker = docs_dir / "General" / "DOC-911" / dr.PARSE_FAILURE_MARKER
    assert marker.exists()
    assert json.loads(marker.read_text())["phase"] == "store"


def test_a_later_success_clears_an_earlier_failure_marker(client, docs_dir, monkeypatch) -> None:
    import mantisfetch_docreader as dr

    real = dr._persist_source_file
    calls = {"n": 0}

    def _boom_once(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("no space left on device")
        return real(*a, **kw)

    # Patched for both calls rather than undone between them: monkeypatch.undo()
    # would also drop this test's docs_dir redirect and send the second write at
    # whatever library the process is really configured for.
    monkeypatch.setattr(dr, "_persist_source_file", _boom_once)
    assert _store(client, doc_id="DOC-912").status_code == 500

    assert _store(client, doc_id="DOC-912", replace="true").status_code == 200
    assert not (docs_dir / "General" / "DOC-912" / dr.PARSE_FAILURE_MARKER).exists()


def test_a_parsed_document_has_no_text_window(client, docs_dir) -> None:
    """The window reads the whole original to slice it, and a parsed document's
    original can be 200 MiB. It also has sections, which is the reader it should
    be using."""
    doc_id = client.post(
        "/doc/parse",
        files={"file": ("p.txt", b"line one\nline two\n", "text/plain")},
        data={"summary_mode": "off", "generate_summary": "false"},
    ).json()["doc_id"]

    resp = client.get(f"/doc/library/{doc_id}/source/info", params={"limit": 1})
    assert resp.status_code == 422
    assert "parsed document" in resp.json()["detail"]
    # Its metadata still answers — only the window is refused.
    assert client.get(f"/doc/library/{doc_id}/source/info").json()["kind"] == "parsed"


def test_a_search_hit_says_which_reader_to_use(client, docs_dir) -> None:
    """ "Search, then read the digest" is the documented flow, and a raw document
    has no digest — so the discriminator has to be on the hit, not only on the
    manifest."""
    raw_id = _store(client, name="plan.md").json()["doc_id"]
    client.post(
        "/doc/parse",
        files={"file": ("plan.html", b"<h1>Plan</h1><p>Body worth keeping.</p>", "text/html")},
        data={"summary_mode": "off", "generate_summary": "false"},
    )

    hits = client.get("/doc/library/search", params={"q": "plan"}).json()["results"]
    kinds = {h["doc_id"]: h["kind"] for h in hits}
    assert kinds[raw_id] == "raw"
    assert set(kinds.values()) == {"raw", "parsed"}


def test_the_mcp_tool_can_store_a_raw_document_too(client, docs_dir, tmp_path, monkeypatch) -> None:
    """MCP is presented as the same service through another door. Leaving
    store_only off it would make markdown ingestable over HTTP and impossible
    over MCP, for an agent that only has the one."""
    import base64

    import mantisfetch_mcp as mm

    out = asyncio.run(
        mm.doc_parse(
            content_b64=base64.b64encode(MD).decode(),
            filename="notes.md",
            store_only=True,
        )
    )
    assert out["kind"] == "raw"
    assert asyncio.run(mm.doc_source(out["doc_id"]))["media_type"] == "text/markdown"


def test_a_raw_write_is_held_against_the_other_writer(client, docs_dir) -> None:
    """The deferred-summary writer is a plain thread and can never take the
    per-doc asyncio lock the request holds; it rewrites the same directory, the
    same .rollback/, the same products. Every other rewrite here goes through
    _reversible_rewrite, which takes the cross-thread lock for exactly that
    reason (#168) — the raw path does not go through it, so it takes the lock
    itself.

    Asserted from inside the other holder: while the lock is held, the raw write
    must not have landed. The timing check below is only a sanity signal; this
    one is the claim.
    """
    import threading

    import mantisfetch_docreader as dr

    holding = threading.Event()
    release = threading.Event()
    seen: dict[str, object] = {}
    doc_dir = docs_dir / "General" / "DOC-920"

    def _hold() -> None:
        with dr._document_writer_lock(docs_dir, "DOC-920"):
            holding.set()
            release.wait(5)
            # Still inside the lock: nothing else may have written here.
            manifest = doc_dir / "manifest.json"
            seen["landed"] = (
                json.loads(manifest.read_text()).get("kind") if manifest.exists() else None
            )

    holder = threading.Thread(target=_hold)
    holder.start()
    assert holding.wait(5)

    done = threading.Event()
    result: dict[str, object] = {}

    def _store_it() -> None:
        result["status"] = _store(client, doc_id="DOC-920").status_code
        done.set()

    writer = threading.Thread(target=_store_it)
    writer.start()
    assert not done.wait(0.4), "the raw write did not wait for the writer lock"

    release.set()
    holder.join(5)
    assert done.wait(5)
    writer.join(5)

    assert seen["landed"] is None, "a raw document landed while another writer held the lock"
    assert result["status"] == 200

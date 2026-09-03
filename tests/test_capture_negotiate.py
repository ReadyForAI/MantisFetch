"""Capture asks the site for markdown before opening a browser.

Two guards decide whether the fast path may run at all, and both exist because
of an interaction rather than a bug in the path itself:

- extract_tables: a negotiated document has no tables, and the URL cache key
  includes extract_tables — so a table-less document would be cached under a key
  promising tables and handed to every later caller who asked for them.
- an existing doc for the URL: the content-hash lookup runs after extraction and
  the two paths do not produce identical blocks, so a page captured one way and
  then the other mints a second doc_id every time, not occasionally.

Losing either guard is silent: nothing errors, and the damage shows up as a
duplicate doc or a document missing tables. They are tested first.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import mantisfetch_browser as lb
import pytest
from mantisfetch_browser import negotiate
from mantisfetch_browser.extract import html_to_blocks, markdown_to_blocks
from starlette.testclient import TestClient

MARKDOWN = """# Mantis shrimp

Mantis shrimp are carnivorous marine crustaceans of the order Stomatopoda.

## Eyes

They carry between twelve and sixteen photoreceptor types in each eye.

## Claws

The second pair of thoracic appendages is adapted for close-range combat.
"""


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_NEGOTIATE_MARKDOWN", "1")


def _negotiated(markdown: str = MARKDOWN, via: str = "negotiated", status: int = 200):
    return negotiate.NegotiatedDoc(markdown, "https://example.com/page", status, via)


def _capture(client: TestClient, docs_dir: Path, *, body=None, **payload):
    """POST /web/capture with the browser mocked and negotiation stubbed."""
    browser = MagicMock()
    context = AsyncMock()
    page = AsyncMock()
    response = MagicMock()
    response.status = 200
    response.url = "https://example.com/page"
    page.goto = AsyncMock(return_value=response)
    context.new_page = AsyncMock(return_value=page)
    browser.new_context = AsyncMock(return_value=context)

    async def fake_fetch(url, *, timeout_ms=10_000):
        if isinstance(body, BaseException):
            raise body
        return body

    distilled = {
        "url": "https://example.com/page", "title": "Browser path",
        "content_hash": "sha256:browser", "sections": [
            {"sid": "s_b", "h": "H", "t": "browser body", "type": "text"}],
        "actions": [], "meta": {},
    }
    with (
        patch("mantisfetch_browser._get_docs_dir", return_value=docs_dir),
        patch("mantisfetch_browser._setup_routing", new=AsyncMock()),
        patch("mantisfetch_browser.negotiate.try_fetch_markdown", new=fake_fetch),
        patch("mantisfetch_browser._distill", new=AsyncMock(return_value=distilled)),
    ):
        orig = lb._browser
        lb._browser = browser
        try:
            resp = client.post("/web/capture", json={"url": "https://example.com/page", **payload})
        finally:
            lb._browser = orig
    return resp, browser


# ── the two guards ──────────────────────────────────────────────────────────────
def test_extract_tables_skips_the_fast_path(client, tmp_path: Path) -> None:
    """Otherwise a table-less document lands in the cache under a key that
    promises tables, and every later caller asking for tables gets it."""
    resp, browser = _capture(client, tmp_path, body=_negotiated(), extract_tables=True)
    assert resp.status_code == 200
    browser.new_context.assert_awaited()  # the browser path ran
    manifest = _manifest(tmp_path, resp)
    assert manifest["provenance"]["fetch_via"] == "html"


def test_an_existing_capture_of_the_url_skips_the_fast_path(client, tmp_path: Path) -> None:
    """The content-hash lookup happens after extraction and the paths do not
    produce identical blocks, so letting both run for one URL mints two ids."""
    lb._persist_web_capture(
        doc_id="WEB-800", url="https://example.com/page", title="Already here",
        sections=[{"sid": "s_1", "h": "H", "t": "body", "type": "text"}],
        digest="d", tags=[], content_hash="sha256:existing", docs_dir=tmp_path,
        content_type="General", extract_tables=False,
        requested_url="https://example.com/page", lang="en-US",
    )
    resp, browser = _capture(client, tmp_path, body=_negotiated(), extract_tables=False)
    assert resp.status_code == 200
    browser.new_context.assert_awaited()


def test_the_flag_is_off_by_default(client, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MANTISFETCH_NEGOTIATE_MARKDOWN", raising=False)
    assert negotiate.fast_path_enabled() is False
    resp, browser = _capture(client, tmp_path, body=_negotiated(), extract_tables=False)
    assert resp.status_code == 200
    browser.new_context.assert_awaited()


# ── the happy path, through the endpoint ────────────────────────────────────────
def _manifest(docs_dir: Path, resp) -> dict:
    doc_id = resp.json()["doc_id"]
    ct = resp.json()["content_type"]
    return json.loads((docs_dir / ct / doc_id / "manifest.json").read_text(encoding="utf-8"))


def test_a_hit_captures_without_opening_a_browser(client, tmp_path: Path) -> None:
    resp, browser = _capture(client, tmp_path, body=_negotiated(), extract_tables=False)
    assert resp.status_code == 200
    browser.new_context.assert_not_awaited()

    manifest = _manifest(tmp_path, resp)
    assert manifest["provenance"]["fetch_via"] == "negotiated"
    titles = [s["title"] for s in manifest["sections"]]
    assert "Eyes" in titles and "Claws" in titles


def test_the_response_reports_the_markdown_url_not_the_requested_one(
    client, tmp_path: Path
) -> None:
    """After a .md rewrite or an llms.txt follow they differ, and a caller who
    reads final_url as "what I asked for" would be wrong."""
    doc = negotiate.NegotiatedDoc(MARKDOWN, "https://example.com/page.md", 200, "md-path")
    resp, _ = _capture(client, tmp_path, body=doc, extract_tables=False)
    assert resp.json()["final_url"] == "https://example.com/page.md"
    manifest = _manifest(tmp_path, resp)
    assert manifest["provenance"]["fetch_via"] == "md-path"


def test_the_real_status_is_recorded_not_a_hardcoded_200(client, tmp_path: Path) -> None:
    resp, _ = _capture(client, tmp_path, body=_negotiated(status=203), extract_tables=False)
    assert resp.json()["http_status"] == 203
    assert _manifest(tmp_path, resp)["provenance"]["http_status"] == 203


def test_no_hit_falls_through_to_the_browser(client, tmp_path: Path) -> None:
    resp, browser = _capture(client, tmp_path, body=None, extract_tables=False)
    assert resp.status_code == 200
    browser.new_context.assert_awaited()
    assert _manifest(tmp_path, resp)["provenance"]["fetch_via"] == "html"


def test_a_failing_origin_is_not_retried_with_a_browser(client, tmp_path: Path) -> None:
    """Falling back would mean hitting a server that just returned 5xx with a
    much heavier request."""
    refusal = negotiate.NegotiationRefused(503, "https://example.com/page")
    resp, browser = _capture(client, tmp_path, body=refusal, extract_tables=False)
    assert resp.status_code == 502
    assert "503" in resp.json()["detail"]
    browser.new_context.assert_not_awaited()


# ── identity is shared, not copied ──────────────────────────────────────────────
def test_both_paths_agree_on_content_identity() -> None:
    """The same page reached as markdown or as HTML has to hash the same, or
    dedup silently stops working across the two paths."""
    html = """<html><body><article>
      <h1>Mantis shrimp</h1>
      <p>Mantis shrimp are carnivorous marine crustaceans of the order Stomatopoda.</p>
      <h2>Eyes</h2>
      <p>They carry between twelve and sixteen photoreceptor types in each eye.</p>
      <h2>Claws</h2>
      <p>The second pair of thoracic appendages is adapted for close-range combat.</p>
    </article></body></html>"""

    def identity(blocks):
        sections = lb._blocks_to_sections_stable(
            blocks=blocks, max_sections=0, max_section_chars=0, total_budget=0
        )
        return lb._content_identity("Mantis shrimp", sections)

    assert identity(markdown_to_blocks(MARKDOWN)) == identity(html_to_blocks(html))


# ── the ladder itself ───────────────────────────────────────────────────────────
def test_html_with_a_200_is_a_miss_not_a_hit() -> None:
    """Accept is a request header a server may ignore, and an SPA's not-found
    page is 200 + HTML. Treating it as a hit would store an error page."""
    outcome, body, status = negotiate._classify(
        (200, "text/html", "<html>Not found</html>"), "https://e.com/x"
    )
    assert outcome == "miss" and body is None and status == 200


@pytest.mark.parametrize("content_type", ["text/markdown", "text/x-markdown", "text/plain"])
def test_markdown_content_types_are_hits(content_type: str) -> None:
    outcome, body, _ = negotiate._classify((200, content_type, "# T\n\nbody"), "https://e.com/x")
    assert outcome == "hit" and body


def test_an_empty_body_is_a_miss() -> None:
    outcome, _, _ = negotiate._classify((200, "text/markdown", "   \n"), "https://e.com/x")
    assert outcome == "miss"


def test_a_5xx_on_the_requested_url_refuses() -> None:
    with pytest.raises(negotiate.NegotiationRefused):
        negotiate._classify((503, "text/markdown", "x"), "https://e.com/x", refuse_on_5xx=True)


def test_a_5xx_on_a_speculative_probe_is_only_a_miss() -> None:
    """A .md variant or an llms.txt that does not exist gets 500/503 from plenty
    of hosts instead of 404. Refusing on those would turn a page that reads fine
    in a browser into a failed capture."""
    outcome, body, status = negotiate._classify(
        (503, "text/html", "gateway"), "https://e.com/x.md"
    )
    assert outcome == "miss" and body is None and status == 503


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://e.com/docs/x", "https://e.com/docs/x.md"),
        ("https://e.com/docs/x/", "https://e.com/docs/x.md"),
        ("https://e.com/", "https://e.com/index.md"),
        ("https://e.com/a.md", None),
    ],
)
def test_md_path_variant(url: str, expected: str | None) -> None:
    assert negotiate.md_path_variant(url) == expected


def test_ancestor_probing_is_capped() -> None:
    """agent-browser walks every ancestor; on a deep path that is five probes
    before the browser even starts, on the path that usually misses."""
    candidates = negotiate.llms_candidates("https://e.com/a/b/c/d/e", "llms.txt")
    assert len(candidates) == 3
    assert candidates[-1] == "https://e.com/llms.txt"


async def test_llms_full_is_never_used_as_a_pages_body() -> None:
    """It is the whole site in one file. Storing it as some particular URL's
    document stores the wrong content — and because the existing-URL guard then
    sees a capture for that URL, the next attempt takes the browser path and
    mints a second doc_id for the same page. A site that publishes only
    llms-full.txt must fall through to the browser."""
    served = {
        "https://e.com/llms.txt": (200, "text/markdown", "# Site\n\nNo per-page links.\n"),
        "https://e.com/llms-full.txt": (
            200, "text/markdown", "# Whole site\n\nEverything, all pages, one file.\n"),
    }
    fetched: list[str] = []

    async def fake_fetch(client, url, timeout_s):
        fetched.append(url)
        return served.get(url)

    with patch("mantisfetch_browser.negotiate._fetch", new=fake_fetch):
        assert await negotiate.try_fetch_markdown("https://e.com/docs/page") is None
    # the index was read and produced no link for this page; llms-full was not
    # consulted as a substitute
    assert "https://e.com/llms.txt" in fetched
    assert not any(u.endswith("llms-full.txt") for u in fetched)


async def test_an_llms_index_link_is_followed() -> None:
    """The path that should work still works."""
    # Links are matched by path, so what rung 3 adds over rung 2 is reach to a
    # different origin for the same path — a docs site whose markdown lives on a
    # CDN. The local .md variant 404s here, so rung 2 misses first.
    served = {
        "https://e.com/llms.txt": (
            200, "text/markdown", "- [Page](https://cdn.e.com/docs/page.md)\n"),
        "https://cdn.e.com/docs/page.md": (200, "text/markdown", MARKDOWN),
    }

    async def fake_fetch(client, url, timeout_s):
        return served.get(url)

    with patch("mantisfetch_browser.negotiate._fetch", new=fake_fetch):
        doc = await negotiate.try_fetch_markdown("https://e.com/docs/page")
    assert doc is not None
    assert doc.fetch_via == "llms-index"
    assert doc.final_url == "https://cdn.e.com/docs/page.md"


async def test_a_5xx_probe_does_not_abort_the_ladder() -> None:
    """The .md variant 503s; the index still gets its chance, and a miss still
    falls through to the browser rather than failing the capture."""
    async def fake_fetch(client, url, timeout_s):
        if url.endswith(".md"):
            return (503, "text/html", "gateway")
        return None

    with patch("mantisfetch_browser.negotiate._fetch", new=fake_fetch):
        assert await negotiate.try_fetch_markdown("https://e.com/docs/page") is None


async def test_a_5xx_on_the_requested_url_aborts_the_ladder() -> None:
    async def fake_fetch(client, url, timeout_s):
        return (503, "text/html", "down") if url == "https://e.com/docs/page" else None

    with patch("mantisfetch_browser.negotiate._fetch", new=fake_fetch):
        with pytest.raises(negotiate.NegotiationRefused):
            await negotiate.try_fetch_markdown("https://e.com/docs/page")


def test_llms_link_lookup_matches_the_target_page() -> None:
    body = "- [Other](/other.md)\n- [Docs](/docs/x.md)\n"
    assert (
        negotiate.find_llms_link(body, "https://e.com/llms.txt", "https://e.com/docs/x")
        == "https://e.com/docs/x.md"
    )
    assert negotiate.find_llms_link(body, "https://e.com/llms.txt", "https://e.com/nope") is None


async def test_a_link_to_a_private_address_is_refused() -> None:
    """An llms.txt link is a URL the caller never supplied and may point
    anywhere. This path has no route guard behind it."""
    checked: list[str] = []

    async def allowed(url: str) -> bool:
        checked.append(url)
        return False

    with patch("mantisfetch_browser.negotiate._url_allowed_async", new=allowed):
        assert await negotiate._fetch(MagicMock(), "http://10.0.0.5/x.md", 1.0) is None
    assert checked == ["http://10.0.0.5/x.md"]


# ── markdown blocks match the html vocabulary ───────────────────────────────────
def test_markdown_blocks_use_the_same_tags() -> None:
    tags = {b["tag"] for b in markdown_to_blocks(MARKDOWN)}
    assert tags <= {"h1", "h2", "h3", "p", "li", "blockquote", "pre"}


def test_deep_headings_collapse_to_h3() -> None:
    """_blocks_to_sections_stable only breaks on h1-h3, so an h4 would silently
    become body text instead of a section title."""
    blocks = markdown_to_blocks("#### Deep\n\n##### Deeper\n")
    assert [b["tag"] for b in blocks] == ["h3", "h3"]


def test_fenced_code_keeps_its_indentation() -> None:
    blocks = markdown_to_blocks("```python\ndef f():\n    return 1\n```\n")
    assert blocks == [{"tag": "pre", "text": "def f():\n    return 1"}]


def test_a_heading_inside_a_fence_is_not_a_heading() -> None:
    blocks = markdown_to_blocks("```\n# Not a heading\n```\n")
    assert [b["tag"] for b in blocks] == ["pre"]

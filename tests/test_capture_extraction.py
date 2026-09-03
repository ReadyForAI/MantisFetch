"""Capture extracts in-process and persists under a storage budget.

Two changes, one goal: the library should hold the page, not a preview of it.

Extraction used to run inside the page. Readability returned
``article.textContent`` — plain text — so every paragraph reached
``_blocks_to_sections_stable`` tagged ``p`` and its heading branch never fired.
Measured on the Mantis shrimp article: 2 sections, 3,041 chars, not one real
heading, from a 553 KB page whose H2s are Description / Claws / Eyes / Ecology /
Systematics.

Persisting used the display budget. Capture built a DistillRequest with no
limits passed, so it stored under the defaults meant for keeping model responses
small: max_section_chars=1800, total_text_budget_chars=12000, max_sections=30.
Measured: exactly 12000 chars on disk, four tables sitting at exactly 1799.

Raising the budget without fixing extraction would have stored one long unnamed
block; fixing extraction without raising the budget would have stored 12000
chars of a now-well-structured page. Both are needed, which is why they land
together.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import mantisfetch_browser as lb
import pytest
from mantisfetch_browser import CAPTURE_PERSIST_BUDGET, DistillRequest, SectionBudget
from mantisfetch_browser.extract import html_title, html_to_blocks

# A page shaped like the ones that failed: chrome around the article, an H2 tree,
# a Sphinx pilcrow, a MediaWiki edit link, and a nested list.
PAGE = """
<html><head><title>  Example   Page  </title></head>
<body>
  <nav><a href="/a">Home</a><a href="/b">About</a></nav>
  <header>Site banner that is quite long and would otherwise be a block</header>
  <script>var junk = "should never appear";</script>
  <style>.x { color: red }</style>
  <main>
    <h1>The Article Title</h1>
    <p>An opening paragraph with enough characters to clear the floor.</p>
    <h2>Description<a class="edit" href="#">[edit]</a></h2>
    <p>Descriptive prose that is comfortably longer than the minimum.</p>
    <h2>Basic Usage<a class="headerlink" href="#basic">¶</a></h2>
    <pre>import json
json.dumps({"a": 1})</pre>
    <ul>
      <li>A list item long enough to survive the minimum length filter.</li>
      <li>Another item that also clears the bar comfortably.</li>
    </ul>
    <p>Tiny</p>
  </main>
  <aside>A sidebar promo that is long enough to look like real content</aside>
  <footer>Footer text that is also long enough to look like real content</footer>
</body></html>
"""


# ── extraction ──────────────────────────────────────────────────────────────────
def test_headings_survive_extraction() -> None:
    """The whole point: Readability's textContent destroyed these."""
    blocks = html_to_blocks(PAGE)
    headings = [b["text"] for b in blocks if b["tag"] in ("h1", "h2", "h3")]
    assert headings == ["The Article Title", "Description", "Basic Usage"]


def test_permalink_affordances_are_stripped_from_headings() -> None:
    """Sphinx renders a pilcrow inside the heading and MediaWiki an [edit] link;
    both were ending up in stored section titles."""
    blocks = html_to_blocks(PAGE)
    headings = [b["text"] for b in blocks if b["tag"] in ("h1", "h2", "h3")]
    assert not any("[edit]" in h or "¶" in h for h in headings)


def test_chrome_and_scripts_are_dropped() -> None:
    body = " ".join(b["text"] for b in html_to_blocks(PAGE))
    for junk in ("should never appear", "Site banner", "sidebar promo", "Footer text"):
        assert junk not in body


def test_short_body_blocks_are_dropped_but_short_headings_are_not() -> None:
    blocks = html_to_blocks("<body><p>Tiny</p><h2>Hi</h2></body>")
    assert [(b["tag"], b["text"]) for b in blocks] == [("h2", "Hi")]


def test_nested_blocks_are_not_emitted_twice() -> None:
    """A <li> wrapping a <p> would otherwise contribute its text twice."""
    html = "<body><ul><li><p>Inner text that is long enough to keep.</p></li></ul></body>"
    blocks = html_to_blocks(html)
    assert len(blocks) == 1
    assert blocks[0]["text"] == "Inner text that is long enough to keep."


def test_block_vocabulary_matches_the_simple_distiller() -> None:
    """Both paths feed _blocks_to_sections_stable, so they must agree on tags."""
    tags = {b["tag"] for b in html_to_blocks(PAGE)}
    assert tags <= {"h1", "h2", "h3", "p", "li", "blockquote", "pre"}


def test_max_blocks_is_honoured() -> None:
    html = "<body>" + "".join(f"<p>Paragraph number {n} of many.</p>" for n in range(50)) + "</body>"
    assert len(html_to_blocks(html, max_blocks=10)) == 10


def test_empty_or_unusable_html_yields_nothing() -> None:
    """An empty result is the caller's signal to fall back to the simple path."""
    assert html_to_blocks("") == []
    assert html_to_blocks("<html><body></body></html>") == []


def test_title_is_normalised() -> None:
    assert html_title(PAGE) == "Example Page"
    assert html_title("<html><body>no title</body></html>") is None
    assert html_title("") is None


# ── the storage budget ──────────────────────────────────────────────────────────
def _paragraph_blocks(count: int, chars: int = 400) -> list[dict[str, str]]:
    return [{"tag": "p", "text": f"{n:04d} " + "x" * chars} for n in range(count)]


def test_storage_budget_clips_nothing() -> None:
    """200 paragraphs used to come back as 12000 chars with sections at 1800."""
    blocks = _paragraph_blocks(200)
    expected = sum(len(b["text"]) for b in blocks)

    sections = lb._blocks_to_sections_stable(
        blocks=blocks,
        max_sections=CAPTURE_PERSIST_BUDGET.max_sections,
        max_section_chars=CAPTURE_PERSIST_BUDGET.max_section_chars,
        total_budget=CAPTURE_PERSIST_BUDGET.total_text_budget_chars,
    )
    stored = sum(len(s["t"]) for s in sections)

    # joins add "\n\n" between paragraphs within a section, so allow for those
    assert stored >= expected
    assert not any(len(s["t"]) in (1799, 1800) for s in sections)


def test_display_budget_still_clips() -> None:
    """The storage budget must not leak into the model-facing path."""
    blocks = _paragraph_blocks(200)
    sections = lb._blocks_to_sections_stable(
        blocks=blocks, max_sections=30, max_section_chars=1800, total_budget=12000
    )
    stored = sum(len(s["t"]) for s in sections)
    assert stored <= 12000
    # _clip backs off to the last paragraph boundary, so sections land near but
    # not exactly on the limit; what matters is that content was dropped.
    assert stored < sum(len(b["text"]) for b in blocks) / 4
    assert all(len(s["t"]) <= 1800 for s in sections)


def test_unlimited_max_sections_keeps_tables_a_heading_rich_page_would_evict() -> None:
    """Text fills sections_raw before tables are appended, so a heading-rich page
    under the old max_sections=30 would store prose and drop every table — the
    outline fix would have cost the GDP table."""
    blocks: list[dict[str, str]] = []
    for n in range(40):
        blocks.append({"tag": "h2", "text": f"Heading {n}"})
        blocks.append({"tag": "p", "text": f"Body for section {n}, long enough to keep."})
    tables = [
        {"text": f"| A{n} | B{n} |\n| --- | --- |\n| 1 | 2 |", "table_meta": {"rows": 2}}
        for n in range(3)
    ]

    kept = lb._blocks_to_sections_stable(
        blocks=blocks,
        max_sections=CAPTURE_PERSIST_BUDGET.max_sections,
        max_section_chars=CAPTURE_PERSIST_BUDGET.max_section_chars,
        total_budget=CAPTURE_PERSIST_BUDGET.total_text_budget_chars,
        tables=tables,
    )
    assert sum(1 for s in kept if s["type"] == "table") == 3

    evicted = lb._blocks_to_sections_stable(
        blocks=blocks, max_sections=30, max_section_chars=1800, total_budget=12000, tables=tables
    )
    assert sum(1 for s in evicted if s["type"] == "table") == 0


def test_output_budget_pass_is_skipped_when_unlimited() -> None:
    """_apply_total_output_budget rewrites section text from the tail. It never
    bound for capture only because total_text_budget_chars was the smaller of the
    two; lifting that would have made it the new ceiling."""
    from mantisfetch_browser.ranking import _apply_total_output_budget

    sections = [{"sid": "s1", "h": "H", "t": "y" * 50_000, "type": "text"}]
    out, actions, _ = _apply_total_output_budget(
        sections=sections, actions=[], meta={}, total_budget=0,
        min_actions_to_keep=8, name_max=80, selector_max=120,
    )
    assert len(out[0]["t"]) == 50_000
    assert actions == []


def test_output_budget_pass_still_trims_when_budgeted() -> None:
    from mantisfetch_browser.ranking import _apply_total_output_budget

    sections = [{"sid": "s1", "h": "H", "t": "y" * 50_000, "type": "text"}]
    out, _, _ = _apply_total_output_budget(
        sections=sections, actions=[], meta={}, total_budget=18_000,
        min_actions_to_keep=8, name_max=80, selector_max=120,
    )
    assert len(out[0]["t"]) < 50_000


# ── _distill in html mode ───────────────────────────────────────────────────────
def _html_mode_page(html: str = PAGE) -> MagicMock:
    page = AsyncMock()
    page.url = "https://example.com/article"
    page.content = AsyncMock(return_value=html)
    page.title = AsyncMock(return_value="fallback title")
    page.add_script_tag = AsyncMock(side_effect=AssertionError("must not inject"))
    page.evaluate = AsyncMock(return_value=[])
    session = MagicMock()
    session.page = page
    return session


def test_html_mode_never_injects_a_script() -> None:
    """This is what makes a strict Content-Security-Policy a non-event."""
    session = _html_mode_page()
    out = asyncio.run(
        lb._distill(session, DistillRequest(session_id="s", distill_mode="html",
                                            include_actions=False, extract_tables=False))
    )
    session.page.add_script_tag.assert_not_awaited()
    assert out["meta"]["mode"] == "html"
    assert out["meta"]["readability"]["extractor"] == "in_process"


def test_html_mode_produces_sections_named_by_heading() -> None:
    session = _html_mode_page()
    out = asyncio.run(
        lb._distill(session, DistillRequest(session_id="s", distill_mode="html",
                                            include_actions=False, extract_tables=False))
    )
    titles = [s["h"] for s in out["sections"]]
    assert "Description" in titles and "Basic Usage" in titles
    assert out["title"] == "Example Page"


def test_html_mode_falls_back_to_simple_on_empty_html() -> None:
    session = _html_mode_page(html="<html><body></body></html>")

    async def evaluate(script, *args):
        return {"url": "https://example.com", "title": "T",
                "blocks": [{"tag": "p", "text": "Simple distiller output here."}],
                "tables": []}

    session.page.evaluate = AsyncMock(side_effect=evaluate)
    out = asyncio.run(
        lb._distill(session, DistillRequest(session_id="s", distill_mode="html",
                                            include_actions=False, extract_tables=False))
    )
    assert out["meta"]["mode"] == "simple"
    assert out["sections"]


def test_html_mode_still_extracts_tables_in_page() -> None:
    """Tables stay in the page: HTML-to-text loses them."""
    session = _html_mode_page()

    async def evaluate(script, *args):
        assert script == lb.EXTRACT_TABLES_JS
        assert args[0]["maxTableRows"] == CAPTURE_PERSIST_BUDGET.max_table_rows
        return [{"text": "| A | B |\n| --- | --- |\n| 1 | 2 |", "table_meta": {"rows": 2}}]

    session.page.evaluate = AsyncMock(side_effect=evaluate)
    out = asyncio.run(
        lb._distill(
            session,
            DistillRequest(session_id="s", distill_mode="html", include_actions=False),
            budget=CAPTURE_PERSIST_BUDGET,
        )
    )
    assert any(s["type"] == "table" for s in out["sections"])


def test_request_budget_is_used_when_none_is_passed() -> None:
    """Session callers keep their display budget; only capture overrides it."""
    session = _html_mode_page()
    req = DistillRequest(session_id="s", distill_mode="html", include_actions=False,
                         extract_tables=False, total_text_budget_chars=1000)
    out = asyncio.run(lb._distill(session, req))
    assert out["meta"]["budget"]["total_text_budget_chars"] == 1000


# ── capture wiring ──────────────────────────────────────────────────────────────
def test_capture_uses_html_mode_and_the_storage_budget(client) -> None:
    """The two halves have to arrive together at the persist path."""
    import tempfile
    from pathlib import Path

    seen: dict = {}

    async def fake_distill(session, req, *, budget=None):
        seen["mode"] = req.distill_mode
        seen["budget"] = budget
        return {"url": "https://example.com", "title": "T", "content_hash": "sha256:x",
                "sections": [{"sid": "s1", "h": "H", "t": "body", "type": "text"}],
                "actions": [], "meta": {}}

    with tempfile.TemporaryDirectory() as tmp, patch(
        "mantisfetch_browser._get_docs_dir", return_value=Path(tmp)
    ), patch("mantisfetch_browser._distill", new=fake_distill), patch(
        "mantisfetch_browser._setup_routing", new=AsyncMock()
    ):
        response = MagicMock()
        response.status = 200
        response.url = "https://example.com"
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(return_value=response)
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        orig = lb._browser
        lb._browser = MagicMock()
        lb._browser.new_context = AsyncMock(return_value=mock_context)
        try:
            resp = client.post("/web/capture", json={"url": "https://example.com"})
            assert resp.status_code == 200, resp.text
        finally:
            lb._browser = orig

    assert seen["mode"] == "html"
    assert seen["budget"] == CAPTURE_PERSIST_BUDGET


@pytest.mark.parametrize(
    "field", ["max_sections", "max_section_chars", "total_text_budget_chars",
              "total_output_budget_chars"],
)
def test_every_clipping_limit_is_unlimited_in_the_storage_budget(field: str) -> None:
    """Leaving any one of these at its display default silently becomes the new
    ceiling, so the profile is pinned field by field."""
    assert getattr(CAPTURE_PERSIST_BUDGET, field) == 0


def test_storage_budget_keeps_whole_tables() -> None:
    assert CAPTURE_PERSIST_BUDGET.max_table_rows >= 500
    assert CAPTURE_PERSIST_BUDGET.max_tables >= 20


def test_section_budget_defaults_are_the_storage_profile() -> None:
    assert SectionBudget() == CAPTURE_PERSIST_BUDGET

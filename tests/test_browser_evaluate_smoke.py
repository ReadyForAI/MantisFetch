"""Real-Chromium smoke test guarding multi-arg ``page.evaluate`` regressions.

Playwright's signature is ``evaluate(expression, arg=None)`` — it accepts at most
ONE argument. Passing extra positional args raises ``TypeError`` at runtime. The
rest of the suite drives a fake page whose ``evaluate`` accepts ``*args``, so it
cannot catch this class of bug (see code-review A1: the readability-mode table
extractor and WebMCP invoke silently failed in production for two months). This
test launches a real headless Chromium so the genuine Playwright signature is
exercised end to end.

Requires ``playwright install chromium``; each test skips (instead of failing)
when the package or browser binary is unavailable, so CI without a browser stays
green.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")

import pytest_asyncio  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "services" / "browser"))

import mantisfetch_browser as mb  # noqa: E402
from mantisfetch_browser import vision  # noqa: E402
from mantisfetch_browser.models import DistillRequest  # noqa: E402
from mantisfetch_browser.session import Session  # noqa: E402

# Rich enough for Readability to parse (so readability mode does not fall back to
# simple), and carries a numeric table plus a declarative WebMCP form.
PAGE_HTML = """<!DOCTYPE html>
<html><head><title>Quarterly Report</title></head><body>
<article>
  <h1>Quarterly revenue report</h1>
  <p>The finance team publishes revenue figures every quarter so that regional
     managers can compare performance and plan the next period accordingly.</p>
  <p>This quarter the northern region led growth, followed closely by the western
     region, while the southern and eastern regions held steady against forecast.</p>
  <p>Analysts expect the trend to continue provided that supply chain conditions
     remain stable and marketing spend stays within the approved annual budget.</p>
  <table>
    <caption>Revenue by region</caption>
    <tr><th>Region</th><th>Date</th><th>Amount</th><th>Rate</th></tr>
    <tr><td>North</td><td>2024-01-15</td><td>100</td><td>.5</td></tr>
    <tr><td>South</td><td>2024-02-15</td><td>200</td><td>1.5</td></tr>
    <tr><td>East</td><td>2024-03-15</td><td>300</td><td>2.</td></tr>
    <tr><td>West</td><td>2024-04-15</td><td>400</td><td>.75</td></tr>
  </table>
  <form toolname="subscribe" tooldescription="Subscribe to the report">
    <input name="email" type="text" toolparamdescription="Email address" />
    <button type="submit">Subscribe</button>
  </form>
</article>
</body></html>
"""


@pytest_asyncio.fixture
async def page():
    try:
        pw = await async_playwright().start()
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"playwright runtime unavailable: {exc}")
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover - browser not installed
        await pw.stop()
        pytest.skip(f"chromium not installed (run: playwright install chromium): {exc}")
    context = await browser.new_context()
    pg = await context.new_page()
    await pg.set_content(PAGE_HTML)
    try:
        yield pg
    finally:
        await context.close()
        await browser.close()
        await pw.stop()


def _session(pg) -> Session:
    return Session(context=pg.context, page=pg, lang="en")


async def test_distill_simple_extracts_table(page) -> None:
    """DISTILL_SIMPLE_JS runs with a single {extractTables, maxTableRows} object."""
    result = await mb._distill(
        _session(page),
        DistillRequest(session_id="s", distill_mode="simple", include_actions=False),
    )
    tables = [s for s in result["sections"] if s.get("type") == "table"]
    assert tables, "simple-mode distill returned no tables"
    assert "North" in tables[0]["t"]


async def test_distill_readability_extracts_table(page) -> None:
    """EXTRACT_TABLES_JS runs with a single {maxTableRows, maxTables} object."""
    vision._load_readability_js()
    if not vision.READABILITY_AVAILABLE:
        pytest.skip("readability.js asset unavailable")
    result = await mb._distill(
        _session(page),
        DistillRequest(session_id="s", distill_mode="readability", include_actions=False),
    )
    assert result["meta"]["mode"] == "readability", "readability parse fell back to simple"
    tables = [s for s in result["sections"] if s.get("type") == "table"]
    assert tables, "readability-mode distill returned no tables"


async def test_table_stats_exclude_date_column(page) -> None:
    """D5: numeric column stats must skip a date column parseFloat would coerce."""
    result = await mb._distill(
        _session(page),
        DistillRequest(session_id="s", distill_mode="simple", include_actions=False),
    )
    tables = [s for s in result["sections"] if s.get("type") == "table"]
    assert tables
    stats = (tables[0].get("table_meta") or {}).get("stats") or {}
    assert "Amount" in stats, stats  # the real numeric column is counted
    assert "Rate" in stats, stats  # decimal-only cells (.5, 2.) still count
    assert "Date" not in stats, stats  # dates must not be coerced into stats


async def test_webmcp_declarative_invoke_populates_form(page) -> None:
    """WEBMCP_INVOKE_DECLARATIVE_JS runs with a single {toolName, params, autoSubmit}."""
    result = await mb._invoke_webmcp_tool(_session(page), "subscribe", {"email": "a@b.com"})
    assert result["success"] is True, result
    value = await page.eval_on_selector(
        'form[toolname="subscribe"] [name="email"]', "el => el.value"
    )
    assert value == "a@b.com"


async def test_map_box_to_element_accepts_object_arg(page) -> None:
    """MAP_BOX_TO_ELEMENT runs with a single {cx, cy} object (vision fallback path)."""
    info = await page.evaluate(mb.MAP_BOX_TO_ELEMENT, {"cx": 5, "cy": 5})
    # The point may or may not resolve to an interactive element; the regression
    # guard is only that the multi-arg call itself does not raise TypeError.
    assert info is None or isinstance(info, dict)


# ── navigation-table gate ───────────────────────────────────────────────────────
# A page carrying one data table and one navigation box. Modelled on Wikipedia,
# where a `vte` navbox is as rectangular and as full of <th> label cells as a
# real table — structure cannot separate them, only link density can.
NAV_TABLE_HTML = """<!DOCTYPE html>
<html><head><title>Gate</title></head><body><article>
<p>An article about regional revenue that is long enough to be parsed.</p>
<table><caption>Revenue</caption>
  <tr><th>Region</th><th>Amount</th></tr>
  <tr><td><a href="/north">North</a></td><td>1200</td></tr>
  <tr><td><a href="/south">South</a></td><td>1450</td></tr>
  <tr><td><a href="/east">East</a></td><td>1310</td></tr>
</table>
<table class="navbox">
  <tr><th><a href="/a">Related topics</a></th>
      <td><a href="/b">Alpha region</a> <a href="/c">Beta region</a>
          <a href="/d">Gamma region</a> <a href="/e">Delta region</a></td></tr>
  <tr><th><a href="/f">See also here</a></th>
      <td><a href="/g">Epsilon region</a> <a href="/h">Zeta region</a></td></tr>
</table>
</article></body></html>
"""


@pytest_asyncio.fixture
async def nav_page(page):
    await page.set_content(NAV_TABLE_HTML)
    return page


async def test_navigation_table_is_skipped(nav_page) -> None:
    """The navbox is all link text; the data table is not."""
    tables = await nav_page.evaluate(
        mb.EXTRACT_TABLES_JS,
        {"maxTableRows": 100, "maxTables": 20, "navLinkDensity": 0.8},
    )
    joined = " ".join(t["text"] for t in tables)
    assert "Revenue" in joined or "1200" in joined, "the data table was dropped"
    assert "Alpha region" not in joined, "the navigation box was kept"


async def test_the_gate_can_be_disabled(nav_page) -> None:
    """navLinkDensity=1.0 restores the pre-gate behaviour: every visible table."""
    tables = await nav_page.evaluate(
        mb.EXTRACT_TABLES_JS,
        {"maxTableRows": 100, "maxTables": 20, "navLinkDensity": 1.0},
    )
    joined = " ".join(t["text"] for t in tables)
    assert "Alpha region" in joined
    assert len(tables) == 2


async def test_simple_mode_applies_the_same_gate(nav_page) -> None:
    """Both extractors share one gate, so they cannot disagree about a table."""
    result = await mb._distill(
        _session(nav_page),
        DistillRequest(session_id="s", distill_mode="simple", include_actions=False),
    )
    tables = [s for s in result["sections"] if s.get("type") == "table"]
    joined = " ".join(t["t"] for t in tables)
    assert "1200" in joined
    assert "Alpha region" not in joined


async def test_a_data_table_of_links_is_kept(nav_page) -> None:
    """Link density is a share, not a count: a table whose cells are links but
    which also carries real values stays under the threshold."""
    await nav_page.set_content(
        "<html><body><article><p>Long enough paragraph of article prose here.</p>"
        "<table><caption>Cities</caption>"
        "<tr><th>City</th><th>Population</th></tr>"
        '<tr><td><a href="/a">Springfield</a></td><td>117203 residents counted</td></tr>'
        '<tr><td><a href="/b">Shelbyville</a></td><td>241887 residents counted</td></tr>'
        "</table></article></body></html>"
    )
    tables = await nav_page.evaluate(
        mb.EXTRACT_TABLES_JS,
        {"maxTableRows": 100, "maxTables": 20, "navLinkDensity": 0.8},
    )
    assert any("Springfield" in t["text"] for t in tables)

"""A capture must not turn an error page into a library document, and a strict
Content-Security-Policy must not kill the whole capture.

Both were measured on a real v1.6.3 instance:

- fetching a 404 URL returned a doc_id whose digest read "Page not found", with
  nothing in the response to tell the caller the article was never there
- a page served with ``script-src 'self'`` failed the whole capture with
  ``500 capture distill failed: Page.add_script_tag: Executing inline script
  violates ... Content Security Policy``, because the Readability injection was
  unguarded and the existing "simple" fallback only covered an empty parse
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient


def _distill_result(url: str = "https://example.com", sections: list | None = None) -> dict:
    return {
        "url": url,
        "title": "Example",
        "content_hash": "sha256:abc",
        "sections": (
            [{"sid": "s1", "h": "H", "t": "body text", "type": "text"}]
            if sections is None
            else sections
        ),
        "actions": [],
        "meta": {},
    }


def _goto_mock(status: int | None, url: str = "https://example.com") -> AsyncMock:
    if status is None:
        return AsyncMock(return_value=None)
    response = MagicMock()
    response.status = status
    response.url = url
    return AsyncMock(return_value=response)


def _capture(client: TestClient, docs_dir: Path, goto: AsyncMock, sections: list | None = None):
    """Run POST /web/capture with the browser mocked and the given goto."""
    import mantisfetch_browser as lb

    distilled = _distill_result(sections=sections)
    with (
        patch("mantisfetch_browser._get_docs_dir", return_value=docs_dir),
        patch("mantisfetch_browser._distill", new=AsyncMock(return_value=distilled)),
        patch("mantisfetch_browser._setup_routing", new=AsyncMock()),
    ):
        mock_page = AsyncMock()
        mock_page.goto = goto
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        orig = lb._browser
        lb._browser = MagicMock()
        lb._browser.new_context = AsyncMock(return_value=mock_context)
        try:
            return client.post("/web/capture", json={"url": "https://example.com"})
        finally:
            lb._browser = orig


# ── error pages never enter the library ─────────────────────────────────────────
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        # An upstream 4xx is the caller's URL being dead or forbidden. Answering
        # 502 would read as a retryable gateway failure and invite an agent to
        # hammer a dead link, so these are 422.
        (400, 422), (403, 422), (404, 422), (410, 422), (451, 422),
        # An upstream 5xx really is a bad gateway, and retrying may work.
        (500, 502), (502, 502), (503, 502),
    ],
)
def test_error_page_is_rejected_and_not_stored(
    client: TestClient, status: int, expected: int
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        resp = _capture(client, docs_dir, _goto_mock(status))

        assert resp.status_code == expected
        assert str(status) in resp.json()["detail"]
        # nothing was written: no doc dir, no index
        assert not (docs_dir / "General").exists()
        assert not (docs_dir / "doc-index.json").exists()


def test_error_page_detail_names_the_final_url(client: TestClient) -> None:
    """After a redirect the useful URL is where it landed, not what was asked for."""
    with tempfile.TemporaryDirectory() as tmp:
        resp = _capture(
            client, Path(tmp), _goto_mock(404, url="https://example.com/moved/404")
        )
        assert "https://example.com/moved/404" in resp.json()["detail"]


@pytest.mark.parametrize("status", [200, 201, 204, 304, 399])
def test_non_error_status_is_captured(client: TestClient, status: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        resp = _capture(client, Path(tmp), _goto_mock(status))
        assert resp.status_code == 200
        assert resp.json()["http_status"] == status


def test_missing_response_is_not_treated_as_an_error(client: TestClient) -> None:
    """page.goto returns None for a same-document navigation. That is not a 4xx —
    capturing must proceed, with http_status reported as null."""
    with tempfile.TemporaryDirectory() as tmp:
        resp = _capture(client, Path(tmp), _goto_mock(None))
        assert resp.status_code == 200
        assert resp.json()["http_status"] is None


def test_response_reports_final_url(client: TestClient) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        resp = _capture(client, Path(tmp), _goto_mock(200))
        assert resp.json()["final_url"] == "https://example.com"


def test_manifest_records_http_status(client: TestClient) -> None:
    import json

    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        resp = _capture(client, docs_dir, _goto_mock(200))
        doc_id = resp.json()["doc_id"]
        manifest = json.loads(
            (docs_dir / "General" / doc_id / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["provenance"]["http_status"] == 200


def test_manifest_records_null_status_distinctly(client: TestClient) -> None:
    """A navigation with no response writes http_status: null. That has to stay
    distinguishable from a capture made before the field existed, which has no
    key at all — so the key is always written."""
    import json

    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        resp = _capture(client, docs_dir, _goto_mock(None))
        doc_id = resp.json()["doc_id"]
        provenance = json.loads(
            (docs_dir / "General" / doc_id / "manifest.json").read_text(encoding="utf-8")
        )["provenance"]
        assert "http_status" in provenance
        assert provenance["http_status"] is None


def test_reused_response_carries_the_recorded_status(client: TestClient) -> None:
    """A cache hit must not drop http_status: the capture recorded it, so the
    reused response reads it back rather than reporting null."""
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        first = _capture(client, docs_dir, _goto_mock(200))
        assert first.json()["reused"] is False

        second = _capture(client, docs_dir, _goto_mock(200))
        body = second.json()
        assert body["reused"] is True
        assert body["doc_id"] == first.json()["doc_id"]
        assert body["http_status"] == 200


def test_session_goto_reports_status_without_refusing(client: TestClient) -> None:
    """A session may legitimately want to land on a 404 and act from there, so
    goto reports the status instead of rejecting it — the opposite of capture,
    which is about to persist what it fetched."""
    import mantisfetch_browser as lb

    mock_page = AsyncMock()
    mock_page.goto = _goto_mock(404)
    mock_page.title = AsyncMock(return_value="Not Found")
    mock_page.url = "https://example.com/missing"
    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    orig = lb._browser
    lb._browser = MagicMock()
    lb._browser.new_context = AsyncMock(return_value=mock_context)
    try:
        with patch("mantisfetch_browser._setup_routing", new=AsyncMock()):
            sid = client.post("/web/session/new", json={}).json()["session_id"]
            resp = client.post(
                "/web/session/goto",
                json={"session_id": sid, "url": "https://example.com/missing"},
            )
            assert resp.status_code == 200
            assert resp.json()["http_status"] == 404
            client.post("/web/session/close", json={"session_id": sid})
    finally:
        lb._browser = orig


# ── CSP: a blocked injection degrades to simple, it does not abort ──────────────
def _distill_page(readability_js: str = "/* readability */"):
    """A page mock wired for _distill: no Readability present, tables absent."""
    page = AsyncMock()
    page.url = "https://example.com"
    page.title = AsyncMock(return_value="Example")

    async def evaluate(script, *args):
        if script == "typeof Readability !== 'undefined'":
            return False
        # DISTILL_SIMPLE_JS is the only other script reachable in this test
        return {
            "url": "https://example.com",
            "title": "Example",
            "blocks": [
                {"tag": "h2", "text": "A Heading"},
                {"tag": "p", "text": "Some body text that is long enough to keep."},
            ],
            "tables": [],
        }

    page.evaluate = AsyncMock(side_effect=evaluate)
    return page


def test_blocked_script_injection_falls_back_to_simple() -> None:
    """A CSP that forbids inline script used to 500 the whole capture."""
    import asyncio

    import mantisfetch_browser as lb
    from mantisfetch_browser import DistillRequest

    page = _distill_page()
    page.add_script_tag = AsyncMock(
        side_effect=Exception(
            "Page.add_script_tag: Executing inline script violates the following "
            "Content Security Policy directive 'script-src self'"
        )
    )
    session = MagicMock()
    session.page = page

    with (
        patch.object(lb.vision, "READABILITY_AVAILABLE", True),
        patch.object(lb.vision, "READABILITY_JS", "/* readability */"),
    ):
        out = asyncio.run(
            lb._distill(session, DistillRequest(session_id="s", include_actions=False))
        )

    assert out["meta"]["mode"] == "simple"
    assert out["meta"]["readability"]["fallback_reason"] == "script_injection_blocked"
    # and it produced real content rather than an empty shell
    assert any("body text" in s["t"] for s in out["sections"])


def test_blocked_injection_does_not_evaluate_readability() -> None:
    """Falling back must skip READABILITY_EVAL entirely — Readability is undefined
    on the page, so evaluating it would throw and re-raise the 500 we just fixed."""
    import asyncio

    import mantisfetch_browser as lb
    from mantisfetch_browser import DistillRequest

    page = _distill_page()
    page.add_script_tag = AsyncMock(side_effect=Exception("CSP"))
    session = MagicMock()
    session.page = page

    with (
        patch.object(lb.vision, "READABILITY_AVAILABLE", True),
        patch.object(lb.vision, "READABILITY_JS", "/* readability */"),
    ):
        asyncio.run(lb._distill(session, DistillRequest(session_id="s", include_actions=False)))

    evaluated = [c.args[0] for c in page.evaluate.await_args_list]
    assert lb.READABILITY_EVAL not in evaluated


def test_successful_injection_still_uses_readability() -> None:
    """The fallback must not fire when injection works."""
    import asyncio

    import mantisfetch_browser as lb
    from mantisfetch_browser import DistillRequest

    page = AsyncMock()
    page.url = "https://example.com"
    page.title = AsyncMock(return_value="Example")

    async def evaluate(script, *args):
        if script == "typeof Readability !== 'undefined'":
            return False
        if script == lb.READABILITY_EVAL:
            return {
                "title": "Example",
                "url": "https://example.com",
                "byline": None,
                "excerpt": None,
                "siteName": None,
                "text": "First paragraph.\n\nSecond paragraph.",
            }
        return []

    page.evaluate = AsyncMock(side_effect=evaluate)
    page.add_script_tag = AsyncMock()
    session = MagicMock()
    session.page = page

    with (
        patch.object(lb.vision, "READABILITY_AVAILABLE", True),
        patch.object(lb.vision, "READABILITY_JS", "/* readability */"),
    ):
        out = asyncio.run(
            lb._distill(session, DistillRequest(session_id="s", include_actions=False))
        )

    assert out["meta"]["mode"] == "readability"
    assert "fallback_reason" not in out["meta"]["readability"]


# ── a 200 that yields nothing is also not a document ─────────────────────────────
# A v1.7.2 retest force-refreshed https://news.ycombinator.com/ and got a doc_id
# for a capture with 0 sections, 0 tables and an all-but-blank digest. The page
# answered 200, so the 4xx branch above never saw it, and everything downstream
# happily stored the nothing it was handed.
#
# Measured on the live front page (34,539 bytes): zero h1/h2/h3/p/li/blockquote/
# pre, against 4 tables, 98 tr, 159 td. In-process extraction reads exactly the
# first list, so a page laid out entirely in tables yields no text blocks at all
# — true of every table-layout site, and true since the extraction rewrite, not
# something this release changed. What is fixed here is storing the result.


def _empty_sections(kind: str) -> list:
    if kind == "none":
        return []
    if kind == "blank":
        return [{"sid": "s1", "h": "Hacker News", "t": "", "type": "text"}]
    return [{"sid": "s1", "h": "Hacker News", "t": "   \n\t ", "type": "text"}]


@pytest.mark.parametrize("kind", ["none", "blank", "whitespace"])
def test_a_page_that_yields_no_content_is_refused_and_not_stored(
    client: TestClient, kind: str
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        resp = _capture(client, docs_dir, _goto_mock(200), sections=_empty_sections(kind))

        assert resp.status_code == 422
        assert not (docs_dir / "General").exists()
        assert not (docs_dir / "doc-index.json").exists()


def test_the_empty_capture_detail_is_not_the_dead_url_one(client: TestClient) -> None:
    """Two different 422s reach the caller from this endpoint. One means the URL
    is dead, the other that the page had nothing to read. Telling them apart must
    not require the server log."""
    with tempfile.TemporaryDirectory() as tmp:
        empty = _capture(client, Path(tmp), _goto_mock(200), sections=[]).json()["detail"]
    with tempfile.TemporaryDirectory() as tmp:
        dead = _capture(client, Path(tmp), _goto_mock(404)).json()["detail"]

    assert "no extractable content" in empty
    assert "HTTP 200" in empty and "0 text sections" in empty and "0 tables" in empty
    assert "no extractable content" not in dead


def test_a_table_only_page_is_still_a_document(client: TestClient) -> None:
    """The refusal reads section text, and a table's markdown lives in the same
    `t` as prose does. A page whose entire content is one table must therefore
    still capture — this is the half of the rule that has no page in the retests
    to catch it going wrong."""
    table = [
        {
            "sid": "t1",
            "h": "GDP by country",
            "t": "| Country | GDP |\n| --- | --- |\n| Tuvalu | 65 |",
            "type": "table",
        }
    ]
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        resp = _capture(client, docs_dir, _goto_mock(200), sections=table)

        assert resp.status_code == 200
        body = resp.json()
        assert body["table_count"] == 1
        assert (docs_dir / "General" / body["doc_id"] / "manifest.json").exists()

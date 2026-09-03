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


def _distill_result(url: str = "https://example.com") -> dict:
    return {
        "url": url,
        "title": "Example",
        "content_hash": "sha256:abc",
        "sections": [{"sid": "s1", "h": "H", "t": "body text", "type": "text"}],
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


def _capture(client: TestClient, docs_dir: Path, goto: AsyncMock):
    """Run POST /web/capture with the browser mocked and the given goto."""
    import mantisfetch_browser as lb

    with (
        patch("mantisfetch_browser._get_docs_dir", return_value=docs_dir),
        patch("mantisfetch_browser._distill", new=AsyncMock(return_value=_distill_result())),
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
@pytest.mark.parametrize("status", [400, 403, 404, 410, 500, 503])
def test_error_page_is_rejected_and_not_stored(client: TestClient, status: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp)
        resp = _capture(client, docs_dir, _goto_mock(status))

        assert resp.status_code == 502
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

"""The URL a negotiated capture records is the one that answered.

`_fetch` follows redirects itself — up to five hops, checking SSRF on every one
— and then returned only `(status, content_type, body)`. So `try_fetch_markdown`
built the `NegotiatedDoc` with the URL it had *asked* for, and the response and
the manifest recorded a page that had redirected away as the source of a body
served from somewhere else. Every rung of the ladder has the same shape: the
URL as given, the `.md` variant, and a link out of an llms.txt index.

The metric in this file is the other half of the fast path: it was being
incremented under a name the counter table does not have, so `incr` dropped it
and nothing could tell how often the browser was actually being skipped.
"""

import pytest
from mantisfetch_browser import negotiate


@pytest.fixture()
def transport():
    httpx = pytest.importorskip("httpx")

    def handler(request):
        path = request.url.path
        if path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        if path == "/new":
            return httpx.Response(
                200, text="# Moved here\n\nbody", headers={"content-type": "text/markdown"}
            )
        if path == "/page.md":
            return httpx.Response(302, headers={"location": "/final.md"})
        if path == "/final.md":
            return httpx.Response(
                200, text="# Variant\n\nbody", headers={"content-type": "text/markdown"}
            )
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


def _run(monkeypatch, transport, url):
    import asyncio

    httpx = pytest.importorskip("httpx")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport))
    monkeypatch.setattr(negotiate, "_url_allowed_async", _always_allowed)
    return asyncio.run(negotiate.try_fetch_markdown(url))


async def _always_allowed(url):
    return True


def test_the_recorded_url_is_the_one_that_answered(monkeypatch, transport) -> None:
    doc = _run(monkeypatch, transport, "https://example.com/old")

    assert doc is not None
    assert doc.markdown.startswith("# Moved here")
    assert doc.final_url == "https://example.com/new", (
        "recorded the URL that redirected away, not the one that served the body"
    )


def test_the_md_variant_rung_records_its_redirect_too(monkeypatch, transport) -> None:
    """The ladder's second rung asks for <url>.md; that can redirect as well."""
    doc = _run(monkeypatch, transport, "https://example.com/page")

    assert doc is not None and doc.fetch_via == "md-path"
    assert doc.final_url == "https://example.com/final.md"


def test_a_capture_with_no_redirect_is_unchanged(monkeypatch, transport) -> None:
    doc = _run(monkeypatch, transport, "https://example.com/new")

    assert doc is not None
    assert doc.final_url == "https://example.com/new"


# ── the fast path's metric ───────────────────────────────────────────────────────
def test_the_negotiated_hit_counter_exists() -> None:
    """`incr` ignores an unknown name by design, so an unregistered counter is
    silently dropped — the call site looks right and the number never moves."""
    from mantisfetch_common import metrics

    before = metrics.snapshot().get("capture_negotiated_hits")
    metrics.incr("capture_negotiated_hits")
    after = metrics.snapshot().get("capture_negotiated_hits")

    assert before is not None, "counter is not registered"
    assert after == before + 1


def test_the_hit_counter_has_a_denominator() -> None:
    """A hit count on its own cannot say how often the fast path is worth
    having; the attempt count is what makes it a rate."""
    from mantisfetch_common import metrics

    snapshot = metrics.snapshot()
    assert "capture_negotiated_attempts" in snapshot
    assert "capture_negotiated_hits" in snapshot

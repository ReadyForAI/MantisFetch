"""A client disconnect must not throw away a parse that already ran (#199).

The work happens in executor threads, which cannot be cancelled — a disconnect
only unwinds the coroutine waiting on them. Before the fix that meant a parse
could run to completion and then write nothing, leaving an empty document
directory behind.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest


async def _wait_for(path: Path, timeout: float = 10.0) -> bool:
    """Poll for a file the detached handler is expected to write."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return True
        await asyncio.sleep(0.05)
    return False


# ── the decorator itself ───────────────────────────────────────────────────────


async def test_shield_lets_the_handler_finish_after_the_caller_is_cancelled() -> None:
    import mantisfetch_docreader as dr

    ran_to_completion = asyncio.Event()

    @dr._survives_client_disconnect
    async def handler() -> str:
        await asyncio.sleep(0.15)
        ran_to_completion.set()
        return "done"

    task = asyncio.ensure_future(handler())
    await asyncio.sleep(0.02)  # inside the handler, well before it finishes
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(ran_to_completion.wait(), timeout=2.0)


async def test_without_the_shield_the_same_cancel_stops_the_work() -> None:
    """Control: shows the previous test is measuring the shield, not the clock."""
    ran_to_completion = asyncio.Event()

    async def handler() -> str:
        await asyncio.sleep(0.15)
        ran_to_completion.set()
        return "done"

    task = asyncio.ensure_future(handler())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.3)  # past when it would have finished
    assert not ran_to_completion.is_set()


async def test_shield_reraises_immediately_rather_than_waiting() -> None:
    """The client still gets its disconnect at once — the wait is not moved onto it."""
    import mantisfetch_docreader as dr

    @dr._survives_client_disconnect
    async def handler() -> str:
        await asyncio.sleep(3.0)
        return "done"

    task = asyncio.ensure_future(handler())
    await asyncio.sleep(0.02)
    task.cancel()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert loop.time() - t0 < 1.0, "cancellation should not block on the shielded work"


# ── the real /parse handler ────────────────────────────────────────────────────


async def test_parse_still_writes_the_document_after_a_disconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#199 repro: cancel mid-parse, the document must still land in the library."""
    import mantisfetch_docreader as dr

    import mantisfetch_common.storage as cs

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", docs_dir)

    # Make the parse take long enough to be interrupted partway, the way a
    # multi-minute scanned PDF does in production. Sleeping inside the executor
    # thread is what the real parse does — the coroutine is parked on it.
    real_parse_generic = dr.parse_generic

    def slow_parse_generic(*args, **kwargs):
        import time

        time.sleep(0.6)
        return real_parse_generic(*args, **kwargs)

    monkeypatch.setattr(dr, "parse_generic", slow_parse_generic)

    transport = httpx.ASGITransport(app=dr.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://doc.test") as client:
        request = client.post(
            "/parse",
            files={"file": ("note.html", b"<h1>Title</h1><p>Body text.</p>", "text/html")},
            data={"doc_id": "DOC-9001", "summary_mode": "off"},
        )
        task = asyncio.ensure_future(request)
        await asyncio.sleep(0.15)  # parse thread is running; nothing written yet
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    manifest = docs_dir / "General" / "DOC-9001" / "manifest.json"
    assert await _wait_for(manifest), (
        "the parse ran but wrote nothing after the client went away — "
        "this is the #199 failure"
    )
    assert json.loads(manifest.read_text(encoding="utf-8"))["doc_id"] == "DOC-9001"
    assert (docs_dir / "General" / "DOC-9001" / "full.md").exists()


async def test_parse_is_indexed_after_a_disconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Landing on disk is not enough — a document nobody can find is still lost."""
    import mantisfetch_docreader as dr

    import mantisfetch_common.storage as cs

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", docs_dir)

    real_parse_generic = dr.parse_generic

    def slow_parse_generic(*args, **kwargs):
        import time

        time.sleep(0.6)
        return real_parse_generic(*args, **kwargs)

    monkeypatch.setattr(dr, "parse_generic", slow_parse_generic)

    transport = httpx.ASGITransport(app=dr.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://doc.test") as client:
        task = asyncio.ensure_future(
            client.post(
                "/parse",
                files={"file": ("note.html", b"<h1>Indexed</h1><p>Body.</p>", "text/html")},
                data={"doc_id": "DOC-9002", "summary_mode": "off"},
            )
        )
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    manifest = docs_dir / "General" / "DOC-9002" / "manifest.json"
    assert await _wait_for(manifest)
    index = json.loads((docs_dir / "doc-index.json").read_text(encoding="utf-8"))
    ids = {entry["id"] for entry in index.get("documents", [])}
    assert "DOC-9002" in ids, "document is on disk but not in the index"

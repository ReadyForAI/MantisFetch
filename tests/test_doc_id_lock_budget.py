"""Waiting for the same doc_id's lock comes out of the caller's budget (F09).

The handler took the per-doc_id lock with a bare ``async with``, ahead of the
parse slot where the budget lives. A call declaring ``budget_seconds`` while an
earlier parse of the same doc_id ran waited for that parse however long it
took — the MCP client long gone by then — and then started its own parse with
the budget spent.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import mantisfetch_docreader as dr
import pytest
from fastapi import HTTPException
from mantisfetch_docreader.storage import _doc_id_parse_locks

import mantisfetch_common.storage as cs


@pytest.fixture()
def docs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


def _hold(doc_id: str) -> asyncio.Lock:
    """The lock a parse of ``doc_id`` already in progress would be holding.
    The map is weak, so the caller keeps the returned reference."""
    lock = _doc_id_parse_locks.get(doc_id)
    if lock is None:
        lock = asyncio.Lock()
        _doc_id_parse_locks[doc_id] = lock
    return lock


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=dr.app), base_url="http://doc.test")


async def _post(client: httpx.AsyncClient, doc_id: str, **data):
    return await client.post(
        "/parse",
        files={"file": ("note.txt", b"A short note about invoices.", "text/plain")},
        data={"doc_id": doc_id, "summary_mode": "off", **data},
    )


def _scratch_is_empty(docs_dir: Path) -> bool:
    scratch = docs_dir / ".upload-tmp"
    return not scratch.exists() or list(scratch.iterdir()) == []


async def test_a_budget_bounds_the_wait_for_the_same_doc_id(docs_dir: Path) -> None:
    """The report's probe: DOC-123 held, budget 0.05 s. It used to answer 200
    after the lock was released, 631 ms in."""
    lock = _hold("DOC-123")
    await lock.acquire()
    try:
        async with _client() as client:
            started = time.monotonic()
            response = await asyncio.wait_for(
                _post(client, "DOC-123", budget_seconds="0.05"), timeout=3
            )
            took = time.monotonic() - started
    finally:
        lock.release()

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "parse_budget_exceeded"
    assert "this doc_id" in detail["message"]
    assert took < 1.0, f"answered after {took:.2f}s while the lock was held"
    assert not (docs_dir / "General" / "DOC-123").exists()
    assert _scratch_is_empty(docs_dir), "a refusal stranded its upload"


async def test_no_budget_waits_up_to_the_queue_ceiling(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MANTISFETCH_PARSE_QUEUE_MAX_WAIT_SEC", "0.2")
    lock = _hold("DOC-124")
    await lock.acquire()
    try:
        async with _client() as client:
            started = time.monotonic()
            response = await asyncio.wait_for(_post(client, "DOC-124"), timeout=3)
            took = time.monotonic() - started
    finally:
        lock.release()

    assert response.status_code == 429, response.text
    assert "Retry-After" in response.headers
    assert took < 1.0
    assert _scratch_is_empty(docs_dir)


async def test_the_lock_is_not_left_held_by_a_refusal(docs_dir: Path) -> None:
    lock = _hold("DOC-125")
    await lock.acquire()
    async with _client() as client:
        try:
            refused = await asyncio.wait_for(
                _post(client, "DOC-125", budget_seconds="0.05"), timeout=3
            )
        finally:
            lock.release()
        assert refused.status_code == 422
        assert not lock.locked(), "the refused request took the lock with it"
        accepted = await _post(client, "DOC-125", budget_seconds="30")
    assert accepted.status_code == 200, accepted.text


async def test_a_free_lock_is_never_refused_on_a_spent_budget(docs_dir: Path) -> None:
    """The older contract _parse_slot keeps: a caller whose cost could not be
    estimated is not turned away from a gate that is open."""
    async with _client() as client:
        response = await _post(client, "DOC-126", budget_seconds="0.000001")
    assert response.status_code == 200, response.text


async def test_the_slot_wait_shares_the_ceiling_with_the_lock_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One deadline for the queue, not one per wait."""
    monkeypatch.setenv("MANTISFETCH_PARSE_QUEUE_MAX_WAIT_SEC", "5")
    monkeypatch.setattr(dr, "_parse_sem", asyncio.Semaphore(0))  # every slot taken
    started = time.monotonic()
    with pytest.raises(HTTPException) as refused:
        async with dr._parse_slot(
            budget_seconds=None,
            t_entry=started,
            estimate=None,
            queued_since=started - 10,  # already queued past the ceiling
        ):
            pass
    assert refused.value.status_code == 429
    assert time.monotonic() - started < 0.5

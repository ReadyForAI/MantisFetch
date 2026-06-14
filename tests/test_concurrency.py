"""Concurrency tests for TASK-017: counter races, index corruption, session use-after-close."""

import json
import tempfile
import threading
from pathlib import Path

import pytest


class TestDocCounterConcurrency:
    """C2: concurrent _next_doc_id must produce unique IDs."""

    def test_concurrent_doc_ids_are_unique(self):
        from larkscout_docreader import _next_doc_id

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            results: list[str] = []
            errors: list[Exception] = []

            def allocate():
                try:
                    results.append(_next_doc_id(docs_dir))
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=allocate) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"Errors: {errors}"
            assert len(results) == 10
            assert len(set(results)) == 10, f"Duplicate IDs: {results}"

    def test_next_doc_id_skips_existing(self):
        """C8: a counter mint must not reuse an id that already exists on disk."""
        from larkscout_docreader import _next_doc_id

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            existing = docs_dir / "DOC-001"
            existing.mkdir(parents=True)
            (existing / "manifest.json").write_text("{}", encoding="utf-8")
            (docs_dir / ".counter").write_text("1", encoding="utf-8")
            # Counter says 1, but DOC-001 exists → must skip to DOC-002.
            assert _next_doc_id(docs_dir) == "DOC-002"


class TestWebCounterConcurrency:
    """C1: concurrent _next_web_doc_id must produce unique IDs."""

    def test_concurrent_web_ids_are_unique(self):
        from larkscout_browser import _next_web_doc_id

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            results: list[str] = []
            errors: list[Exception] = []

            def allocate():
                try:
                    results.append(_next_web_doc_id(docs_dir))
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=allocate) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"Errors: {errors}"
            assert len(results) == 10
            assert len(set(results)) == 10, f"Duplicate IDs: {results}"


class TestDocIndexIntegrity:
    """C3+C4: concurrent writes to doc-index.json must not lose entries."""

    def test_concurrent_doc_index_writes(self):
        from larkscout_docreader import _update_doc_index

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            errors: list[Exception] = []

            def write_entry(i: int):
                try:
                    meta = {
                        "doc_id": f"DOC-{i:03d}",
                        "filename": f"test_{i}.pdf",
                        "file_type": "pdf",
                        "total_pages": 1,
                        "section_count": 1,
                        "ocr_page_count": 0,
                        "table_count": 0,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                    _update_doc_index(docs_dir, meta, f"digest for {i}")
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=write_entry, args=(i,)) for i in range(1, 11)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"Errors: {errors}"

            index_path = docs_dir / "doc-index.json"
            assert index_path.exists()
            index = json.loads(index_path.read_text())
            doc_ids = {d["id"] for d in index["documents"]}
            expected = {f"DOC-{i:03d}" for i in range(1, 11)}
            assert doc_ids == expected, f"Missing entries: {expected - doc_ids}"

    def test_web_and_doc_share_one_index_lock(self):
        """C6: /web and /doc must serialize on the SAME lock for doc-index.json.

        They run in one process and write the same file; two disjoint locks
        (the old _web_index_lock vs _doc_index_lock) allowed lost updates when a
        capture and a parse interleaved.
        """
        import larkscout_browser
        import larkscout_docreader.storage as doc_storage

        from larkscout_common.storage import _doc_index_lock as common_lock

        assert larkscout_browser._doc_index_lock is common_lock
        assert doc_storage._doc_index_lock is common_lock


class TestSessionEviction:
    """C16: eviction/expiry still closes the session (now outside the lock)."""

    def test_eviction_closes_evicted_session(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from larkscout_browser import Session, SessionManager

        async def run():
            mgr = SessionManager(ttl=60, maxsize=1)
            ctx1 = AsyncMock()
            s1 = Session(context=ctx1, page=MagicMock(), lang="en")
            s2 = Session(context=AsyncMock(), page=MagicMock(), lang="en")
            await mgr.put("a", s1)
            await mgr.put("b", s2)  # exceeds maxsize → evicts "a"
            assert s1.closed
            ctx1.close.assert_awaited()
            assert await mgr.get("a") is None
            assert await mgr.get("b") is not None

        asyncio.run(run())


class TestSessionClosedFlag:
    """H2: session use-after-close — closed sessions must be rejected."""

    def test_closed_session_returns_404(self):
        import asyncio

        from larkscout_browser import Session, SessionManager

        async def run():
            mgr = SessionManager(ttl=60, maxsize=10)
            from unittest.mock import AsyncMock, MagicMock

            ctx = AsyncMock()
            page = MagicMock()
            sess = Session(context=ctx, page=page, lang="en")

            await mgr.put("s_test", sess)

            # Retrieve OK
            got = await mgr.get("s_test")
            assert got is not None
            assert not got.closed

            # Remove → marks closed
            await mgr.remove("s_test")
            assert sess.closed

            # Subsequent get returns None
            got2 = await mgr.get("s_test")
            assert got2 is None

        asyncio.run(run())

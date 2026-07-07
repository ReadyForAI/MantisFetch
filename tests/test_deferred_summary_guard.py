"""Deferred-summary robustness: generation guard (B1) + timeout honoured (A3)."""

import json
import time
from pathlib import Path

import pytest


def _parsed(text: str):
    from mantisfetch_docreader import ParsedDocument, Section

    return ParsedDocument(
        filename="t.pdf",
        file_type="pdf",
        total_pages=1,
        pages=[],
        sections=[
            Section(
                index=1,
                title="Intro",
                level=1,
                text=text,
                page_range="1-1",
                sid="s_abc",
                summary="",
            )
        ],
        ocr_page_count=0,
        table_count=0,
    )


# ---------------------------------------------------------------- B1 guard


def test_guard_skips_write_when_content_hash_differs(tmp_path: Path) -> None:
    """A stale deferred writer must not roll back a newer replace=true parse."""
    from mantisfetch_docreader import write_output, write_output_extract_only

    # Newer parse already on disk (content "NEW").
    write_output_extract_only("DOC-G1", _parsed("NEW content"), tmp_path, source="upload")
    before = (tmp_path / "DOC-G1" / "manifest.json").read_text(encoding="utf-8")

    # Stale deferred thread holding the OLD snapshot tries to write back.
    write_output(
        "DOC-G1",
        _parsed("OLD content"),
        "STALE_DIGEST",
        "STALE_BRIEF",
        tmp_path,
        source="upload",
        guard_stale_generation=True,
    )

    after = (tmp_path / "DOC-G1" / "manifest.json").read_text(encoding="utf-8")
    assert after == before, "stale deferred write overwrote a newer parse"
    assert "STALE_DIGEST" not in (tmp_path / "DOC-G1" / "digest.md").read_text(encoding="utf-8")


def test_guard_skips_write_when_only_metadata_differs(tmp_path: Path) -> None:
    """Generation token catches replaces that change tags/metadata even if text matches."""
    from mantisfetch_docreader import write_output, write_output_extract_only

    write_output_extract_only(
        "DOC-G5", _parsed("same text"), tmp_path, source="upload", tags=["new-tag"]
    )
    before = (tmp_path / "DOC-G5" / "manifest.json").read_text(encoding="utf-8")

    write_output(
        "DOC-G5",
        _parsed("same text"),
        "STALE_DIGEST",
        "STALE_BRIEF",
        tmp_path,
        source="upload",
        tags=["old-tag"],
        guard_stale_generation=True,
    )
    after = (tmp_path / "DOC-G5" / "manifest.json").read_text(encoding="utf-8")
    assert after == before, "stale write rolled back a metadata-only replace"
    assert "STALE_DIGEST" not in (tmp_path / "DOC-G5" / "digest.md").read_text(encoding="utf-8")


def test_guard_allows_write_when_content_hash_matches(tmp_path: Path) -> None:
    from mantisfetch_docreader import write_output, write_output_extract_only

    write_output_extract_only("DOC-G2", _parsed("SAME content"), tmp_path, source="upload")
    write_output(
        "DOC-G2",
        _parsed("SAME content"),
        "FRESH_DIGEST",
        "FRESH_BRIEF",
        tmp_path,
        source="upload",
        guard_stale_generation=True,
    )
    assert "FRESH_DIGEST" in (tmp_path / "DOC-G2" / "digest.md").read_text(encoding="utf-8")


def test_extract_only_guard_skips_stale_placeholder_write(tmp_path: Path) -> None:
    """The deferred running/failed placeholder writes must not roll back a newer parse."""
    from mantisfetch_docreader import write_output_extract_only

    write_output_extract_only("DOC-G4", _parsed("NEW content"), tmp_path, source="upload")
    before = (tmp_path / "DOC-G4" / "manifest.json").read_text(encoding="utf-8")

    write_output_extract_only(
        "DOC-G4",
        _parsed("OLD content"),
        tmp_path,
        source="upload",
        summary_placeholder="(Summary failed: boom)",
        guard_stale_generation=True,
    )
    after = (tmp_path / "DOC-G4" / "manifest.json").read_text(encoding="utf-8")
    assert after == before, "stale failed placeholder overwrote a newer parse"


def test_guard_skips_write_when_manifest_missing(tmp_path: Path) -> None:
    # A guarded write is only ever a deferred-summary update of a doc whose parse
    # already wrote the manifest synchronously; a missing manifest means the doc was
    # deleted (DELETE /library/{doc_id}) mid-flight, so the guard must skip rather
    # than resurrect it. Non-deferred first writes never set guard_stale_generation.
    from mantisfetch_docreader import write_output

    write_output(
        "DOC-G3",
        _parsed("fresh"),
        "FRESH_DIGEST",
        "FRESH_BRIEF",
        tmp_path,
        source="upload",
        guard_stale_generation=True,
    )
    assert not (tmp_path / "DOC-G3" / "manifest.json").exists()


# ---------------------------------------------------------------- A3 timeout


def test_deferred_summary_timeout_does_not_block_on_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """future.result timeout must not join the LLM worker via shutdown(wait=True)."""
    import mantisfetch_docreader as dr

    def slow(parsed, concurrency, flag):  # noqa: ANN001, ARG001
        time.sleep(1.0)  # far past the deadline below
        return ("d", "b", None)

    monkeypatch.setattr(dr, "generate_summaries", slow)
    monkeypatch.setattr(dr, "DEFERRED_SUMMARY_TIMEOUT_SEC", 0.2)

    # The parse writes the manifest synchronously before the deferred thread runs;
    # mirror that so the thread's guarded writes update an existing doc (a missing
    # manifest now means the doc was deleted, which the guard skips).
    dr.write_output_extract_only("DOC-T1", _parsed("timeout content"), tmp_path, source="upload")

    t0 = time.monotonic()
    dr._generate_deferred_summary("DOC-T1", _parsed("timeout content"), tmp_path, 1, None, None, None)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.9, f"deferred summary blocked on worker shutdown ({elapsed:.2f}s)"
    manifest = json.loads((tmp_path / "DOC-T1" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parse_metadata"]["summary"]["status"] == "failed"


def test_semaphore_held_until_worker_finishes_after_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2b: on timeout the concurrency slot must stay held by the abandoned worker,
    not released early — otherwise a hung backend bypasses the concurrency cap."""
    import threading

    import mantisfetch_docreader as dr

    sem = threading.Semaphore(1)
    monkeypatch.setattr(dr, "_deferred_summary_sem", sem)
    monkeypatch.setattr(dr, "DEFERRED_SUMMARY_TIMEOUT_SEC", 0.2)

    release = threading.Event()

    def slow(parsed, concurrency, flag):  # noqa: ANN001, ARG001
        release.wait(3.0)
        return ("d", "b", None)

    monkeypatch.setattr(dr, "generate_summaries", slow)

    # Manifest written synchronously by the parse before the deferred thread (a
    # missing manifest now means the doc was deleted, which the guard skips).
    dr.write_output_extract_only("DOC-T2", _parsed("x"), tmp_path, source="upload")

    dr._generate_deferred_summary("DOC-T2", _parsed("x"), tmp_path, 1, None, None, None)

    # Returned on timeout while the worker is still blocked → slot must stay held.
    assert sem.acquire(blocking=False) is False, "slot released before worker finished"

    release.set()  # let the worker finish → completion callback releases the slot
    freed = False
    for _ in range(60):
        if sem.acquire(blocking=False):
            freed = True
            sem.release()
            break
        time.sleep(0.05)
    assert freed, "slot not released after worker finished"

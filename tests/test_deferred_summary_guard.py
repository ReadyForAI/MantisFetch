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


def test_guard_allows_first_write_when_no_manifest(tmp_path: Path) -> None:
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
    assert (tmp_path / "DOC-G3" / "manifest.json").exists()


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

    t0 = time.monotonic()
    dr._generate_deferred_summary("DOC-T1", _parsed("timeout content"), tmp_path, 1, None, None, None)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.9, f"deferred summary blocked on worker shutdown ({elapsed:.2f}s)"
    manifest = json.loads((tmp_path / "DOC-T1" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parse_metadata"]["summary"]["status"] == "failed"

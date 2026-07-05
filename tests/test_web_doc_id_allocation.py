"""A7: _next_web_doc_id must skip ids already on disk so a reset .web_counter
can't silently overwrite an existing capture (parity with docreader)."""

from pathlib import Path

from mantisfetch_browser import _next_web_doc_id


def test_next_web_doc_id_skips_existing_manifest(tmp_path: Path) -> None:
    # An existing capture occupies WEB-001 (content-type dir layout) while the
    # counter still points at 1 — the pre-fix code would have re-minted WEB-001.
    doc_dir = tmp_path / "General" / "WEB-001"
    doc_dir.mkdir(parents=True)
    (doc_dir / "manifest.json").write_text("{}", encoding="utf-8")

    assert _next_web_doc_id(tmp_path) == "WEB-002"
    assert _next_web_doc_id(tmp_path) == "WEB-003"


def test_next_web_doc_id_skips_flat_layout_manifest(tmp_path: Path) -> None:
    doc_dir = tmp_path / "WEB-001"
    doc_dir.mkdir(parents=True)
    (doc_dir / "manifest.json").write_text("{}", encoding="utf-8")

    assert _next_web_doc_id(tmp_path) == "WEB-002"

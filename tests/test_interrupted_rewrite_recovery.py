"""A rewrite the process did not survive is settled on the next start (F02).

``_restore_on_failure`` recovers from exceptions. A SIGKILL, an OOM or a power
cut is not one: what it leaves is a document whose products have been moved
into ``.rollback/`` while its manifest and index still describe them, and the
next rewrite of that document used to start by deleting that directory — the
only copy of the previous version left.

Whether the rewrite committed is not visible in the directory on its own: a
stashed source with no ``.rollback`` is both "staged, never committed" and
"committed, stash not yet dropped". The marker written at commit time is what
separates the two, and these tests walk every state it has to tell apart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import mantisfetch_docreader as dr
import pytest
from mantisfetch_docreader.models import ParsedDocument, Section

import mantisfetch_common.storage as cs
from mantisfetch_common.doc_index_store import get_document, search_fts

ROOT = Path(__file__).parent.parent


@pytest.fixture()
def docs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


def _parsed(body: str) -> ParsedDocument:
    return ParsedDocument(
        filename="doc.html",
        file_type="html",
        total_pages=1,
        pages=[],
        sections=[
            Section(
                index=1, title="S", level=1, text=body,
                page_range="1-1", sid="s_x", summary="",
            )
        ],
        ocr_page_count=0,
        table_count=0,
    )


def _write(docs_dir: Path, doc_id: str, body: str) -> None:
    dr.write_output(
        doc_id, _parsed(body), f"digest {body}", f"brief {body}", docs_dir,
        source="upload", content_type="General",
    )


def _child(docs_dir: Path, doc_id: str, kill_at: str) -> int:
    """Rewrite a document in a child process that exits hard partway through.

    ``kill_at`` names the function the child replaces with ``os._exit``:
    ``_update_doc_index`` stops it before the index commit, with staging done
    and the new products already written. ``"after"`` lets the whole rewrite
    commit and stops before the caller drops the stash — the other side of the
    same window, and the one the directory alone cannot distinguish.
    """
    script = textwrap.dedent(
        f"""
        import os, sys
        sys.path[:0] = [{str(ROOT)!r},
                        {str(ROOT / "services" / "browser")!r},
                        {str(ROOT / "services" / "docreader")!r}]
        import mantisfetch_docreader as dr
        import mantisfetch_common.storage as cs
        from pathlib import Path
        docs = Path({str(docs_dir)!r})
        cs.DEFAULT_DOCS_DIR = docs
        sys.path.insert(0, {str(ROOT / "tests")!r})
        from test_interrupted_rewrite_recovery import _parsed
        dr._stash_source(docs / "General" / {doc_id!r})
        if {kill_at!r} != "after":
            setattr(dr, {kill_at!r}, lambda *a, **k: os._exit(73))
        dr.write_output(
            {doc_id!r}, _parsed("REPLACEMENT"), "d", "b", docs,
            source="upload", content_type="General",
        )
        os._exit(73 if {kill_at!r} == "after" else 0)
        """
    )
    return subprocess.run([sys.executable, "-c", script], capture_output=True).returncode


def test_a_rewrite_killed_before_its_commit_is_undone_on_the_next_start(
    docs_dir: Path,
) -> None:
    _write(docs_dir, "DOC-7001", "ORIGINAL Zarquonium")
    doc = docs_dir / "General" / "DOC-7001"
    (doc / "source").mkdir(parents=True, exist_ok=True)
    (doc / "source" / "doc.html").write_bytes(b"<p>ORIGINAL</p>")

    assert _child(docs_dir, "DOC-7001", "_update_doc_index") == 73
    assert (doc / ".rollback").exists(), "the child did not reach the staged state"

    rolled_back, committed = dr._finish_interrupted_rewrites(docs_dir)
    assert (rolled_back, committed) == (1, 0)

    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8")
    assert "REPLACEMENT" not in (doc / "full.md").read_text(encoding="utf-8")
    assert (doc / "sections.json").exists()
    assert (doc / "source" / "doc.html").read_bytes() == b"<p>ORIGINAL</p>"
    assert not (doc / ".rollback").exists()
    assert not (doc / ".rollback-source").exists()
    assert get_document(docs_dir, "DOC-7001")["digest"].startswith("digest ORIGINAL")
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-7001"]


def test_a_rewrite_killed_after_its_commit_keeps_the_new_version(docs_dir: Path) -> None:
    """The other half: a committed replacement must not be rolled back."""
    _write(docs_dir, "DOC-7002", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7002"
    (doc / "source").mkdir(parents=True, exist_ok=True)
    (doc / "source" / "doc.html").write_bytes(b"<p>ORIGINAL</p>")

    assert _child(docs_dir, "DOC-7002", "after") == 73
    assert (doc / ".rewrite-committed").exists(), "the commit marker was never written"
    assert (doc / ".rollback-source").exists(), "the child did not reach the stashed state"

    rolled_back, committed = dr._finish_interrupted_rewrites(docs_dir)
    assert (rolled_back, committed) == (0, 1)

    assert "REPLACEMENT" in (doc / "full.md").read_text(encoding="utf-8")
    assert not (doc / ".rollback-source").exists()
    assert not (doc / ".rewrite-committed").exists()


def test_a_stashed_source_alone_goes_back_when_nothing_committed(docs_dir: Path) -> None:
    """Killed between stashing the source and staging the products."""
    _write(docs_dir, "DOC-7003", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7003"
    (doc / "source").mkdir(parents=True, exist_ok=True)
    (doc / "source" / "doc.html").write_bytes(b"<p>ORIGINAL</p>")
    dr._stash_source(doc)
    (doc / "source").mkdir(parents=True, exist_ok=True)
    (doc / "source" / "new.html").write_bytes(b"<p>REPLACEMENT</p>")

    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0)
    assert (doc / "source" / "doc.html").read_bytes() == b"<p>ORIGINAL</p>"
    assert not (doc / "source" / "new.html").exists()
    assert not (doc / ".rollback-source").exists()


def test_a_marker_left_without_scaffolding_is_just_cleared(docs_dir: Path) -> None:
    """Killed after the backup went but before the marker did: nothing to undo."""
    _write(docs_dir, "DOC-7004", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7004"
    (doc / ".rewrite-committed").touch()

    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 1)
    assert not (doc / ".rewrite-committed").exists()
    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8")


def test_the_sweep_is_idempotent_and_leaves_a_settled_library_alone(docs_dir: Path) -> None:
    _write(docs_dir, "DOC-7005", "ORIGINAL")
    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 0)

    assert _child(docs_dir, "DOC-7005", "_update_doc_index") == 73
    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0)
    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 0)
    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 0)
    doc = docs_dir / "General" / "DOC-7005"
    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8")


def test_the_next_rewrite_no_longer_deletes_the_only_copy(docs_dir: Path) -> None:
    """The hazard itself: a rewrite used to open by rmtree-ing the backup."""
    _write(docs_dir, "DOC-7006", "ORIGINAL Zarquonium")
    doc = docs_dir / "General" / "DOC-7006"
    assert _child(docs_dir, "DOC-7006", "_update_doc_index") == 73
    backup = doc / ".rollback"
    assert (backup / "sections.json").exists()

    # No sweep. Straight into another rewrite, which stages over the leftovers
    # and then fails — so what it puts back has to be the previous version,
    # not the half-written one the dead process left in the directory.
    with pytest.raises(RuntimeError):
        with dr._restore_on_failure(
            doc, include_extracted=True, docs_dir=docs_dir, doc_id="DOC-7006"
        ):
            raise RuntimeError("this rewrite fails too")

    assert (doc / "sections.json").exists(), "the previous version's products are gone"
    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8")
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-7006"]


def test_staging_interrupted_halfway_puts_back_only_what_moved(docs_dir: Path) -> None:
    """No `.staged.json` means staging never finished — delete nothing.

    The artifacts that had not been moved yet are still the live ones, and the
    overwritten files were copied, so their originals never left.
    """
    _write(docs_dir, "DOC-7008", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7008"
    cache = doc / ".cache"
    cache.mkdir(exist_ok=True)
    (cache / "ocr_p0001.abc.txt").write_text("a page nothing staged", encoding="utf-8")

    backup = doc / ".rollback"
    backup.mkdir()
    os.replace(doc / "sections.json", backup / "sections.json")
    (backup / ".cache").mkdir()
    (backup / ".cache" / "search_full.lower.txt").write_text("old", encoding="utf-8")

    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0)
    assert json.loads((doc / "sections.json").read_text(encoding="utf-8"))
    assert (cache / "ocr_p0001.abc.txt").read_text(encoding="utf-8") == "a page nothing staged"
    assert not backup.exists()

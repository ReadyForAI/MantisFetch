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
import time
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

    rolled_back, committed, incomplete = dr._finish_interrupted_rewrites(docs_dir)
    assert (rolled_back, committed, incomplete) == (1, 0, 0)

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

    rolled_back, committed, incomplete = dr._finish_interrupted_rewrites(docs_dir)
    assert (rolled_back, committed, incomplete) == (0, 1, 0)

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

    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0, 0)
    assert (doc / "source" / "doc.html").read_bytes() == b"<p>ORIGINAL</p>"
    assert not (doc / "source" / "new.html").exists()
    assert not (doc / ".rollback-source").exists()


def test_a_marker_left_without_scaffolding_is_just_cleared(docs_dir: Path) -> None:
    """Killed after the backup went but before the marker did: nothing to undo."""
    _write(docs_dir, "DOC-7004", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7004"
    (doc / ".rewrite-committed").touch()

    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 1, 0)
    assert not (doc / ".rewrite-committed").exists()
    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8")


def test_the_sweep_is_idempotent_and_leaves_a_settled_library_alone(docs_dir: Path) -> None:
    _write(docs_dir, "DOC-7005", "ORIGINAL")
    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 0, 0)

    assert _child(docs_dir, "DOC-7005", "_update_doc_index") == 73
    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0, 0)
    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 0, 0)
    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 0, 0)
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

    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0, 0)
    assert json.loads((doc / "sections.json").read_text(encoding="utf-8"))
    assert (cache / "ocr_p0001.abc.txt").read_text(encoding="utf-8") == "a page nothing staged"
    assert not backup.exists()


# ── The sweep's own crash and failure boundaries ──────────────────────────────


def test_a_restore_interrupted_halfway_can_be_resumed(docs_dir: Path) -> None:
    """The sweep can die too, and the second run must not finish the damage.

    A name it has already put back is no longer in the backup. Deleting that
    target before looking would remove the only copy left.
    """
    _write(docs_dir, "DOC-7101", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7101"
    assert _child(docs_dir, "DOC-7101", "_update_doc_index") == 73

    # First pass restores sections.json and stops before the rest.
    backup = doc / ".rollback"
    dr._put_back_staged(doc, backup, ["sections.json"])
    assert (doc / "sections.json").exists()
    assert not (backup / "sections.json").exists()

    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0, 0)
    assert (doc / "sections.json").exists(), "the resumed sweep deleted what it had restored"
    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8")


def _commit_the_index_the_child_never_reached(
    docs_dir: Path, doc: Path, digest: str, content_hash: str
) -> None:
    """The index write the killed child stopped short of, with no marker."""
    from mantisfetch_docreader.storage import _update_doc_index

    meta = json.loads((doc / ".meta.json").read_text(encoding="utf-8"))
    _update_doc_index(
        docs_dir, meta, digest, content_hash=content_hash, content_type="General"
    )
    assert not (doc / ".rewrite-committed").exists()


def test_a_commit_with_no_marker_is_still_read_as_committed(docs_dir: Path) -> None:
    """The marker lands just after the commit, and "just after" has a gap.

    Every commit stamps a new write_id, and the backup holds the row as it was
    before the rewrite: a row still carrying the snapshot's id is a commit that
    never landed, marker or no marker.
    """
    _write(docs_dir, "DOC-7102", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7102"
    assert _child(docs_dir, "DOC-7102", "_update_doc_index") == 73

    manifest = json.loads((doc / "manifest.json").read_text(encoding="utf-8"))
    _commit_the_index_the_child_never_reached(
        docs_dir, doc, "digest REPLACEMENT", manifest["provenance"]["content_hash"]
    )

    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 1, 0)
    assert "REPLACEMENT" in (doc / "full.md").read_text(encoding="utf-8"), (
        "a committed rewrite was rolled back under the index"
    )


def test_a_commit_that_did_not_change_the_content_is_still_a_commit(
    docs_dir: Path,
) -> None:
    """A rewrite that only redoes the summary leaves the content hash alone.

    Reading the content would call that uncommitted however late it died, and
    roll the files back under an index row holding the new digest.
    """
    _write(docs_dir, "DOC-7105", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7105"
    was = json.loads((doc / "manifest.json").read_text(encoding="utf-8"))
    assert _child(docs_dir, "DOC-7105", "_update_doc_index") == 73

    # Same content hash on both sides — only the digest is new.
    _commit_the_index_the_child_never_reached(
        docs_dir, doc, "a freshly written digest", was["provenance"]["content_hash"]
    )

    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 1, 0)
    assert "REPLACEMENT" in (doc / "full.md").read_text(encoding="utf-8")


def test_a_commit_with_no_content_hash_at_all_is_still_a_commit(
    docs_dir: Path,
) -> None:
    """A raw replacement's content hash is the empty string on both sides."""
    _write(docs_dir, "DOC-7106", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7106"
    assert _child(docs_dir, "DOC-7106", "_update_doc_index") == 73
    snapshot = json.loads((doc / ".rollback" / ".index-before.json").read_text(encoding="utf-8"))
    snapshot["content_hash"] = ""
    (doc / ".rollback" / ".index-before.json").write_text(
        json.dumps(snapshot), encoding="utf-8"
    )

    _commit_the_index_the_child_never_reached(docs_dir, doc, "digest REPLACEMENT", "")

    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 1, 0)
    assert "REPLACEMENT" in (doc / "full.md").read_text(encoding="utf-8")


def test_a_status_update_on_the_row_is_not_mistaken_for_a_commit(
    docs_dir: Path,
) -> None:
    """Not every upsert is a rewrite.

    A web capture's deferred summary writes its status onto the row it already
    has. Comparing whole rows would read that as the rewrite committing, and
    finalize one that never did — deleting the only copy of the old version.
    """
    _write(docs_dir, "DOC-7107", "ORIGINAL Zarquonium")
    doc = docs_dir / "General" / "DOC-7107"
    assert _child(docs_dir, "DOC-7107", "_update_doc_index") == 73

    from mantisfetch_common.doc_index_store import get_document, upsert_document

    row = get_document(docs_dir, "DOC-7107")
    row["summary_status"] = "completed"
    upsert_document(docs_dir, row)

    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0, 0)
    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8")
    assert (doc / "sections.json").exists()


def test_a_stash_that_will_not_move_keeps_its_marker(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup that fails must leave the state readable, not half-cleared.

    Dropping the marker while the stash is still there turns a committed
    replacement into an uncommitted one on the next start, and the old source
    goes back over the new.
    """
    _write(docs_dir, "DOC-7103", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7103"
    (doc / "source").mkdir(parents=True, exist_ok=True)
    (doc / "source" / "doc.html").write_bytes(b"<p>ORIGINAL</p>")
    assert _child(docs_dir, "DOC-7103", "after") == 73
    (doc / "source").mkdir(parents=True, exist_ok=True)
    (doc / "source" / "new.html").write_bytes(b"<p>REPLACEMENT</p>")

    real_move = dr.shutil.move

    def refuse_the_stash(src, dst, *a, **k):
        if dr._SOURCE_ROLLBACK_DIR in str(src):
            raise OSError("the stash will not move")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(dr.shutil, "move", refuse_the_stash)
    dr._finish_interrupted_rewrites(docs_dir)
    monkeypatch.undo()

    assert (doc / ".rewrite-committed").exists(), "the marker went while the stash stayed"

    # The next start settles it, and the committed source is the one that survives.
    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 1, 0)
    assert (doc / "source" / "new.html").read_bytes() == b"<p>REPLACEMENT</p>"


def test_staging_interrupted_before_its_snapshot_leaves_the_text_searchable(
    docs_dir: Path,
) -> None:
    """A missing snapshot means "no indexed text" only once staging finished.

    Interrupted before it, the row still holds the document's own text, and
    restoring "nothing" would delete it.
    """
    _write(docs_dir, "DOC-7104", "ORIGINAL Zarquonium")
    doc = docs_dir / "General" / "DOC-7104"
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-7104"]

    (doc / ".rollback").mkdir()  # created, nothing written into it yet

    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0, 0)
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-7104"], (
        "the sweep dropped a search row it had no snapshot for"
    )


def test_the_committed_verdict_is_written_down_before_its_evidence_goes(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep that stops halfway must not leave the next one guessing.

    The verdict is read from the backup's snapshot. Delete the backup first and
    a sweep interrupted before it clears the stash leaves `.rollback-source`
    alone on disk — which the next start reads as a rewrite that never
    committed, putting the old source back over the new one.
    """
    _write(docs_dir, "DOC-7108", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7108"
    (doc / "source").mkdir(parents=True, exist_ok=True)
    (doc / "source" / "doc.html").write_bytes(b"<p>ORIGINAL</p>")
    assert _child(docs_dir, "DOC-7108", "_update_doc_index") == 73
    (doc / "source").mkdir(parents=True, exist_ok=True)
    (doc / "source" / "new.html").write_bytes(b"<p>REPLACEMENT</p>")
    manifest = json.loads((doc / "manifest.json").read_text(encoding="utf-8"))
    _commit_the_index_the_child_never_reached(
        docs_dir, doc, "digest REPLACEMENT", manifest["provenance"]["content_hash"]
    )

    def stop_before_clearing_the_stash(*a, **k):
        raise OSError("the sweep stops here")

    monkeypatch.setattr(dr, "_discard_stashed_source", stop_before_clearing_the_stash)
    dr._finish_interrupted_rewrites(docs_dir)
    monkeypatch.undo()

    assert (doc / ".rewrite-committed").exists(), "the verdict was not recorded"
    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 1, 0)
    assert (doc / "source" / "new.html").read_bytes() == b"<p>REPLACEMENT</p>"


def test_clearing_the_stash_keeps_the_marker_while_a_backup_remains(
    docs_dir: Path,
) -> None:
    """The upload paths call this directly, so the rule lives here.

    A successful write that could not remove `.rollback` keeps its marker on
    purpose. Dropping it in the stash cleanup that follows would hand the next
    start a backup with nothing to say it had already committed.
    """
    _write(docs_dir, "DOC-7109", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7109"
    (doc / ".rewrite-committed").touch()
    (doc / ".rollback").mkdir()
    (doc / ".rollback-source").mkdir()

    dr._discard_stashed_source(doc)

    assert not (doc / ".rollback-source").exists()
    assert (doc / ".rewrite-committed").exists(), (
        "the marker went while a backup was still there"
    )


def test_a_marker_the_last_write_could_not_clear_does_not_condemn_the_next(
    docs_dir: Path,
) -> None:
    """A stale marker must not be read as evidence about a different rewrite.

    A successful write that cannot unlink its marker leaves it behind. The next
    rewrite stages over it, and if that one dies before committing, the sweep
    would take the old marker as proof it had — and delete the only backup.
    """
    _write(docs_dir, "DOC-7110", "ORIGINAL Zarquonium")
    doc = docs_dir / "General" / "DOC-7110"
    (doc / ".rewrite-committed").touch()  # the unlink that did not happen

    # The next rewrite stages, then dies before its own commit.
    assert _child(docs_dir, "DOC-7110", "_update_doc_index") == 73

    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0, 0)
    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8"), (
        "the stale marker finalized a rewrite that never committed"
    )
    assert (doc / "sections.json").exists()


def test_a_commit_that_cannot_be_recorded_keeps_its_backup(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup carries the snapshot that proves the commit landed.

    Deleting it after a marker write that failed leaves a state nothing can
    read: the next start sees a stash with no marker and no snapshot, and puts
    the old source back under the new manifest.
    """
    _write(docs_dir, "DOC-7111", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7111"

    real_touch = Path.touch

    def refuse_the_marker(self, *a, **k):
        if self.name == ".rewrite-committed":
            raise OSError("no space left on device")
        return real_touch(self, *a, **k)

    monkeypatch.setattr(Path, "touch", refuse_the_marker)
    with dr._restore_on_failure(
        doc, include_extracted=True, docs_dir=docs_dir, doc_id="DOC-7111"
    ):
        pass
    monkeypatch.undo()

    assert (doc / ".rollback" / ".index-before.json").exists(), (
        "the snapshot the next start needs was deleted"
    )


def test_a_first_write_that_commits_is_not_rolled_back(docs_dir: Path) -> None:
    """A document with no index row yet still has a state worth recording.

    "No snapshot" used to mean both "there was no row" and "nothing was
    written down", so a first upload that committed and died before its marker
    was read as uncommitted — and the sweep cleared its searchable text.
    """
    doc = docs_dir / "General" / "DOC-7112"
    doc.mkdir(parents=True)
    with dr._restore_on_failure(
        doc, include_extracted=True, docs_dir=docs_dir, doc_id="DOC-7112"
    ):
        assert (doc / ".rollback" / ".index-before.json").exists(), (
            "a document with no index row yet was staged without a snapshot"
        )
        _write(docs_dir, "DOC-7112", "FIRST Zarquonium")
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-7112"]

    # Replay the crash window: the commit landed, the marker did not. The
    # scaffolding is rebuilt exactly as the staging above wrote it.
    (doc / ".rewrite-committed").unlink(missing_ok=True)
    backup = doc / ".rollback"
    backup.mkdir(exist_ok=True)
    dr._write_json(backup / ".index-before.json", {})
    dr._write_json(backup / ".staged.json", {"staged": []})

    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 1, 0)
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-7112"], (
        "a committed first write lost its searchable text"
    )


def test_a_search_index_that_will_not_write_keeps_the_backup_for_a_retry(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files back, search table not: that has to stay fixable."""
    _write(docs_dir, "DOC-7113", "ORIGINAL Zarquonium")
    doc = docs_dir / "General" / "DOC-7113"
    assert _child(docs_dir, "DOC-7113", "_update_doc_index") == 73

    import mantisfetch_common.doc_index_store as dis

    def refuse_the_write(*a, **k):
        raise RuntimeError("the search table will not take it")

    monkeypatch.setattr(dis, "upsert_fts", refuse_the_write)
    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 0, 1)
    monkeypatch.undo()

    assert (doc / ".rollback").exists(), "nothing is left to retry from"
    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0, 0)
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-7113"]
    assert not (doc / ".rollback").exists()


# ── A read that failed is not a read that found nothing ───────────────────────


def test_an_index_that_could_not_be_read_at_staging_settles_nothing(
    docs_dir: Path,
) -> None:
    """An unread snapshot used to be written as "no row".

    The existing row's write_id then compared unequal to the missing one, and
    a replacement that died before committing was finalized — the backup with
    the only copy of the original deleted.
    """
    _write(docs_dir, "DOC-7114", "ORIGINAL Zarquonium")
    doc = docs_dir / "General" / "DOC-7114"
    assert _child(docs_dir, "DOC-7114", "_update_doc_index") == 73
    # What staging records when the index read fails.
    dr._write_json(doc / ".rollback" / ".index-before.json", dr._INDEX_UNKNOWN)

    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0, 0)
    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8")
    assert (doc / "sections.json").exists()


def test_staging_records_an_unreadable_index_as_unknown(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(docs_dir, "DOC-7115", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7115"
    import mantisfetch_common.doc_index_store as dis

    def the_index_will_not_read(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(dis, "get_document", the_index_will_not_read)
    with dr._restore_on_failure(doc, include_extracted=True, docs_dir=docs_dir, doc_id="DOC-7115"):
        recorded = json.loads((doc / ".rollback" / ".index-before.json").read_text(encoding="utf-8"))
    assert recorded == dr._INDEX_UNKNOWN, "an unread index was recorded as an absent row"


def test_a_staging_record_that_will_not_read_leaves_the_backup_alone(
    docs_dir: Path,
) -> None:
    """There and unreadable is not "staging never finished".

    Taken for an interrupted staging, the sweep restored only the moved
    artifacts, left the replacement's full.md in place, and deleted the backup
    holding the original one.
    """
    _write(docs_dir, "DOC-7116", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7116"
    assert _child(docs_dir, "DOC-7116", "_update_doc_index") == 73
    (doc / ".rollback" / ".staged.json").write_text("{not json", encoding="utf-8")

    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 0, 1)
    assert (doc / ".rollback" / "full.md").exists(), "the backup holding the original went"


def test_an_unsettled_document_refuses_the_next_rewrite(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging over evidence the sweep chose to keep would destroy it.

    A refused write is recoverable; the snapshot needed to repair the
    document's search text, once deleted, is not.
    """
    _write(docs_dir, "DOC-7117", "ORIGINAL Zarquonium")
    doc = docs_dir / "General" / "DOC-7117"
    assert _child(docs_dir, "DOC-7117", "_update_doc_index") == 73

    import mantisfetch_common.doc_index_store as dis

    def refuse_the_write(*a, **k):
        raise RuntimeError("the search table will not take it")

    monkeypatch.setattr(dis, "upsert_fts", refuse_the_write)
    with pytest.raises(RuntimeError, match="could not be settled"):
        with dr._restore_on_failure(
            doc, include_extracted=True, docs_dir=docs_dir, doc_id="DOC-7117"
        ):
            pass
    monkeypatch.undo()

    assert (doc / ".rollback" / ".fts-before.txt").exists(), "the repair snapshot was deleted"
    assert dr._finish_interrupted_rewrites(docs_dir) == (1, 0, 0)
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-7117"]


# ── Round 5: no new rewrite starts on top of an unsettled one ─────────────────


def _parse(client, content: bytes, doc_id: str, **extra):
    return client.post(
        "/parse",
        files={"file": ("doc.html", content, "text/html")},
        data={"doc_id": doc_id, "summary_mode": "off", **extra},
    )


def _store(client, content: bytes, doc_id: str, **extra):
    return client.post(
        "/parse",
        files={"file": ("notes.md", content, "text/markdown")},
        data={"doc_id": doc_id, "store_only": "true", **extra},
    )


def _leave_a_committed_replacement_with_its_stash(doc: Path) -> None:
    """What a cleanup failure most often leaves: the commit landed, the marker
    says so, and the stash of the source it replaced was never dropped."""
    stash = doc / ".rollback-source"
    stash.mkdir()
    (stash / "previous.md").write_bytes(b"# the source this one replaced")
    (doc / ".rewrite-committed").touch()


def _leave_something_that_cannot_be_settled(doc: Path) -> None:
    """A backup whose staging record will not read: recovery must not guess."""
    stash = doc / ".rollback-source"
    stash.mkdir()
    (stash / "notes.md").write_bytes(b"# THE ONLY COPY")
    (doc / ".rollback").mkdir()
    (doc / ".rollback" / ".staged.json").write_text("{not json", encoding="utf-8")


def _source_bytes(doc: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted((doc / "source").iterdir())}


def test_a_raw_replace_settles_a_committed_leftover_and_goes_ahead(docs_dir: Path) -> None:
    """Codex's round-6 P1 scenario. It used to answer 500 — and the failure
    recorder still put the stale stash back over the committed source."""
    from starlette.testclient import TestClient

    client = TestClient(dr.app, raise_server_exceptions=False)
    assert _store(client, b"# first", "DOC-7121").status_code == 200
    doc = docs_dir / "General" / "DOC-7121"
    _leave_a_committed_replacement_with_its_stash(doc)

    response = _store(client, b"# second", "DOC-7121", replace="true")

    assert response.status_code == 200, response.text
    assert _source_bytes(doc)["notes.md"] == b"# second"
    for leftover in (".rollback", ".rollback-source", ".rewrite-committed"):
        assert not (doc / leftover).exists(), leftover


def test_a_raw_replace_that_cannot_settle_leaves_the_document_alone(docs_dir: Path) -> None:
    """Refused means untouched: same source bytes, same stash, same backup."""
    from starlette.testclient import TestClient

    client = TestClient(dr.app, raise_server_exceptions=False)
    assert _store(client, b"# first", "DOC-7122").status_code == 200
    doc = docs_dir / "General" / "DOC-7122"
    _leave_something_that_cannot_be_settled(doc)
    before = _source_bytes(doc)

    response = _store(client, b"# second", "DOC-7122", replace="true")

    assert response.status_code == 500
    assert "could not be settled" in response.text
    assert _source_bytes(doc) == before, "a refused replace changed the source"
    assert (doc / ".rollback-source" / "notes.md").read_bytes() == b"# THE ONLY COPY"
    assert (doc / ".rollback" / ".staged.json").exists()


def test_a_parsed_replace_settles_a_committed_leftover_and_goes_ahead(docs_dir: Path) -> None:
    from starlette.testclient import TestClient

    client = TestClient(dr.app, raise_server_exceptions=False)
    assert _parse(client, b"<p>ORIGINAL</p>", "DOC-7118").status_code == 200
    doc = docs_dir / "General" / "DOC-7118"
    _leave_a_committed_replacement_with_its_stash(doc)

    response = _parse(client, b"<p>REPLACEMENT</p>", "DOC-7118", replace="true")

    assert response.status_code == 200, response.text
    assert "REPLACEMENT" in (doc / "full.md").read_text(encoding="utf-8")
    for leftover in (".rollback", ".rollback-source", ".rewrite-committed"):
        assert not (doc / leftover).exists(), leftover


def test_a_parsed_replace_that_cannot_settle_leaves_the_document_alone(docs_dir: Path) -> None:
    from starlette.testclient import TestClient

    client = TestClient(dr.app, raise_server_exceptions=False)
    assert _parse(client, b"<p>ORIGINAL</p>", "DOC-7123").status_code == 200
    doc = docs_dir / "General" / "DOC-7123"
    _leave_something_that_cannot_be_settled(doc)
    before = _source_bytes(doc)

    response = _parse(client, b"<p>REPLACEMENT</p>", "DOC-7123", replace="true")

    assert response.status_code == 500
    assert _source_bytes(doc) == before
    assert (doc / ".rollback-source" / "notes.md").read_bytes() == b"# THE ONLY COPY"
    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8")


def test_a_previous_marker_that_will_not_clear_blocks_the_next_rewrite(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(docs_dir, "DOC-7119", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7119"
    (doc / ".rewrite-committed").touch()

    real_unlink = Path.unlink

    def refuse_the_marker(self, *a, **k):
        if self.name == ".rewrite-committed":
            raise OSError("read-only file system")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse_the_marker)
    with pytest.raises(RuntimeError, match="could not be settled"):
        with dr._restore_on_failure(
            doc, include_extracted=True, docs_dir=docs_dir, doc_id="DOC-7119"
        ):
            pass
    monkeypatch.undo()
    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8")


def test_the_committed_stash_is_gone_before_the_deferred_summary_starts(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker rewrites the document; a stash it found would read as its own."""
    from starlette.testclient import TestClient

    client = TestClient(dr.app, raise_server_exceptions=False)
    assert _parse(client, b"<p>ORIGINAL</p>", "DOC-7120").status_code == 200
    doc = docs_dir / "General" / "DOC-7120"

    seen: list[bool] = []
    monkeypatch.setattr(
        dr,
        "_generate_deferred_summary",
        lambda *a, **k: seen.append((doc / ".rollback-source").exists()),
    )
    response = _parse(
        client, b"<p>REPLACEMENT</p>", "DOC-7120", replace="true", summary_mode="defer"
    )
    assert response.status_code == 200
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    assert seen == [False], "the deferred rewrite started with the previous stash still here"


def test_staging_a_source_clears_a_stale_marker_first(docs_dir: Path) -> None:
    """A deferred summary can finish between the settle and the stash and
    leave its marker behind; the replacement's stash must not sit under it."""
    _write(docs_dir, "DOC-7124", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7124"
    (doc / "source").mkdir(exist_ok=True)
    (doc / "source" / "doc.html").write_bytes(b"<p>ORIGINAL</p>")
    (doc / ".rewrite-committed").touch()

    dr._stash_source(doc)

    assert not (doc / ".rewrite-committed").exists(), "the stash was staged under a stale marker"
    assert (doc / ".rollback-source" / "doc.html").read_bytes() == b"<p>ORIGINAL</p>"


def test_a_marker_that_will_not_clear_stops_the_stash_before_it_moves_anything(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(docs_dir, "DOC-7125", "ORIGINAL")
    doc = docs_dir / "General" / "DOC-7125"
    (doc / "source").mkdir(exist_ok=True)
    (doc / "source" / "doc.html").write_bytes(b"<p>ORIGINAL</p>")
    (doc / ".rewrite-committed").touch()
    real_unlink = Path.unlink

    def refuse_the_marker(self, *a, **k):
        if self.name == ".rewrite-committed":
            raise OSError("read-only file system")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse_the_marker)
    with pytest.raises(RuntimeError, match="commit marker"):
        dr._stash_source(doc)
    monkeypatch.undo()

    assert (doc / "source" / "doc.html").read_bytes() == b"<p>ORIGINAL</p>"
    assert not (doc / ".rollback-source").exists()


def _a_rollback_delete_that_dies_after_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """rmtree of a backup that removes `.fts-before.txt` and then stops — the
    process dying partway through the delete."""
    import shutil as shutil_module

    real_rmtree = shutil_module.rmtree

    def dies_partway(path, *a, **k):
        if Path(path).name == ".rollback":
            (Path(path) / ".fts-before.txt").unlink(missing_ok=True)
            return None
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(dr.shutil, "rmtree", dies_partway)


def test_a_sweep_that_dies_deleting_its_backup_keeps_the_search_text(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(docs_dir, "DOC-7126", "ORIGINAL Zarquonium")
    doc = docs_dir / "General" / "DOC-7126"
    assert _child(docs_dir, "DOC-7126", "_update_doc_index") == 73

    _a_rollback_delete_that_dies_after_the_snapshot(monkeypatch)
    dr._finish_interrupted_rewrites(docs_dir)
    monkeypatch.undo()
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-7126"]

    dr._finish_interrupted_rewrites(docs_dir)  # the next start
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-7126"], (
        "the second sweep deleted the search text the first one put back"
    )
    assert not (doc / ".rollback").exists()


def test_an_in_process_rollback_that_dies_deleting_its_backup_keeps_the_search_text(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(docs_dir, "DOC-7127", "ORIGINAL Zarquonium")
    doc = docs_dir / "General" / "DOC-7127"

    _a_rollback_delete_that_dies_after_the_snapshot(monkeypatch)
    with pytest.raises(RuntimeError):
        with dr._restore_on_failure(
            doc, include_extracted=True, docs_dir=docs_dir, doc_id="DOC-7127"
        ):
            raise RuntimeError("the rewrite fails")
    monkeypatch.undo()

    dr._finish_interrupted_rewrites(docs_dir)  # the next start
    assert search_fts(docs_dir, "Zarquonium") == ["DOC-7127"], (
        "the sweep deleted the search text the in-process rollback put back"
    )
    assert "ORIGINAL" in (doc / "full.md").read_text(encoding="utf-8")

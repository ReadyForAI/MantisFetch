"""A delete cannot be undone by a summary that was already running (#168).

The deferred-summary writer decides whether to write by looking for the
manifest, and PR #167 made a missing one mean "the document was deleted, skip".
That closed the wide window but not the check itself: a delete landing between
the look and the write was still undone, and the document came back after the
caller had been told it was gone.
"""

from __future__ import annotations

import threading
from pathlib import Path

import mantisfetch_docreader as dr
import pytest
from starlette.testclient import TestClient

import mantisfetch_common.storage as cs


@pytest.fixture()
def docs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


def _parsed(text: str) -> dr.ParsedDocument:
    return dr.ParsedDocument(
        filename="doc.html",
        file_type="html",
        total_pages=1,
        pages=[],
        sections=[
            dr.Section(
                index=1, title="S", level=1, text=text,
                page_range="1-1", sid="s_x", summary="",
            )
        ],
        ocr_page_count=0,
        table_count=0,
    )


def test_a_delete_landing_mid_write_is_not_undone_by_the_summary(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete has to outlast a summary write it interrupted.

    The deferred writer holds the snapshot that is on disk, so it passes the
    guard by design and goes on to write. A delete arriving in that window used
    to be undone by the write that followed it.
    """
    snapshot = _parsed("ORIGINAL")
    dr.write_output_extract_only(
        "DOC-7001", snapshot, docs_dir, source="upload", content_type="General"
    )
    doc = docs_dir / "General" / "DOC-7001"
    assert (doc / "manifest.json").exists()

    inside_the_window = threading.Event()
    let_it_finish = threading.Event()
    real_skip = dr._skip_stale_generation
    verdicts: list[bool] = []

    def pause_after_deciding(*args, **kwargs):
        verdict = real_skip(*args, **kwargs)
        verdicts.append(verdict)
        inside_the_window.set()
        let_it_finish.wait(5)
        return verdict

    monkeypatch.setattr(dr, "_skip_stale_generation", pause_after_deciding)

    summary_writer = threading.Thread(
        target=dr.write_output,
        args=("DOC-7001", snapshot, "DEFERRED_DIGEST", "DEFERRED_BRIEF", docs_dir),
        kwargs={"source": "upload", "content_type": "General", "guard_stale_generation": True},
    )
    summary_writer.start()
    assert inside_the_window.wait(5), "the summary writer never reached the guard"

    deleted: list[object] = []
    with TestClient(dr.app) as client:
        deleter = threading.Thread(
            target=lambda: deleted.append(client.delete("/library/DOC-7001").json())
        )
        deleter.start()
        # Give the delete every chance to land *inside* the window — that is the
        # interleaving under test. Serialized it cannot, and waits here instead;
        # the release below is what lets it through, and it must still stick.
        deleter.join(0.5)

        let_it_finish.set()
        summary_writer.join(10)
        deleter.join(10)
        assert not summary_writer.is_alive() and not deleter.is_alive()

    assert verdicts == [False], f"the summary writer skipped instead of racing: {verdicts}"
    assert deleted and deleted[0]["deleted"] is True
    assert not doc.exists(), "the summary write brought the deleted document back"
    assert not any(
        d.get("id") == "DOC-7001" for d in dr._load_doc_index(docs_dir)
    ), "the document is gone from disk but still in the index"


def test_a_summary_that_starts_after_a_delete_still_skips(
    docs_dir: Path
) -> None:
    """Control for the other arrival order — the #167 behaviour must survive.

    Whichever of the two takes the lock first, the document stays deleted.
    """
    snapshot = _parsed("ORIGINAL")
    dr.write_output_extract_only(
        "DOC-7002", snapshot, docs_dir, source="upload", content_type="General"
    )

    with TestClient(dr.app) as client:
        assert client.delete("/library/DOC-7002").json()["deleted"] is True

    dr.write_output(
        "DOC-7002", snapshot, "DEFERRED_DIGEST", "DEFERRED_BRIEF", docs_dir,
        source="upload", content_type="General", guard_stale_generation=True,
    )

    assert not (docs_dir / "General" / "DOC-7002").exists()

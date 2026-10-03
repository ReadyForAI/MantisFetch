"""A background summary write never commits beside a replacement's stash (#309).

A replacement stashes the old source and persists the new one before it writes
the document. The previous version's deferred summary can finish in that
window: its generation still matches the manifest on disk — the one being
replaced — so it wrote, left a commit marker, and kept it because a stash was
present. A process that died before the replacement committed then had the
startup sweep read that marker as the replacement's commit: the old source was
thrown away while full.md and the manifest stayed on the old version.

Real /parse, real guarded writer, real sweep; only the timing of the old
summary's completion is controlled. Same filename on both versions, so a
differently named history file cannot hide the loss.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import mantisfetch_docreader as dr
import pytest
from starlette.testclient import TestClient

import mantisfetch_common.storage as cs
from mantisfetch_common.doc_index_store import get_document

ROOT = Path(__file__).parent.parent
DOC = "DOC-8101"


@pytest.fixture()
def docs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


_CHILD = textwrap.dedent(
    """
    import json, os, sys, threading
    from pathlib import Path
    from unittest.mock import patch
    sys.path[:0] = [{root!r}, {root!r} + "/services/browser", {root!r} + "/services/docreader"]
    import mantisfetch_docreader as dr
    import mantisfetch_common.storage as cs
    from starlette.testclient import TestClient

    docs = Path({docs!r}); cs.DEFAULT_DOCS_DIR = docs
    doc = docs / "General" / {doc!r}
    mode = {mode!r}
    captured = []
    c = TestClient(dr.app, raise_server_exceptions=False)
    with patch.object(dr, "_generate_deferred_summary", lambda *a, **k: captured.append(a)):
        r = c.post("/parse", files={{"file": ("doc.html", b"<p>ORIGINAL</p>", "text/html")}},
                   data={{"doc_id": {doc!r}, "summary_mode": "defer"}})
    assert r.status_code == 200, r.text
    a = captured[0]

    def old_summary_completes():
        dr.write_output(a[0], a[1], "ORIGINAL summary completed", "original brief", a[2],
                        tags=a[4], metadata=a[5], source_record=a[6], content_type=a[7],
                        source="upload", guard_stale_generation=True)
        print("BEFORE_KILL", json.dumps({{
            "marker": (doc / ".rewrite-committed").exists(),
            "summary_landed": "ORIGINAL summary completed" in (doc / "digest.md").read_text(),
        }}), flush=True)

    real_stash, real_persist = dr._stash_source_locked, dr._persist_source_file

    def stash_then_summary(*args, **kwargs):
        real_stash(*args, **kwargs)
        old_summary_completes()
        if mode == "after_stash":
            os._exit(73)

    def persist_then_summary(*args, **kwargs):
        record = real_persist(*args, **kwargs)
        old_summary_completes()
        if mode == "after_persist":
            os._exit(73)
        return record

    patches = [patch.object(dr, "_persist_source_file", persist_then_summary)]
    if mode == "after_stash":
        patches = [patch.object(dr, "_stash_source_locked", stash_then_summary)]
    if mode == "after_commit":
        # The replacement commits, then dies before it drops its stash.
        patches.append(patch.object(dr, "_discard_stashed_source", lambda *a, **k: os._exit(73)))
    for p in patches:
        p.start()
    c.post("/parse", files={{"file": ("doc.html", b"<p>REPLACEMENT</p>", "text/html")}},
           data={{"doc_id": {doc!r}, "summary_mode": "off", "replace": "true"}})
    os._exit(0)
    """
)


def _run(docs_dir: Path, tmp_path: Path, mode: str) -> dict:
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(root=str(ROOT), docs=str(docs_dir), doc=DOC, mode=mode))
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert done.returncode == 73, (done.returncode, done.stderr[-2000:])
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("BEFORE_KILL ")]
    return json.loads(lines[-1].split(" ", 1)[1]) if lines else {}


def _consistent(docs_dir: Path, version: str) -> None:
    doc = docs_dir / "General" / DOC
    source = (doc / "source" / "doc.html").read_bytes()
    manifest = json.loads((doc / "manifest.json").read_text(encoding="utf-8"))
    full = (doc / "full.md").read_text(encoding="utf-8")
    assert version in source.decode(), f"source is {source!r}"
    assert version in full, f"full.md does not hold {version}"
    assert manifest["provenance"]["source_sha256"] == hashlib.sha256(source).hexdigest(), (
        "the manifest describes a different source than the one on disk"
    )
    assert get_document(docs_dir, DOC)["source_sha256"] == hashlib.sha256(source).hexdigest()
    for leftover in (".rollback", ".rollback-source", ".rewrite-committed"):
        assert not (doc / leftover).exists(), leftover


@pytest.mark.parametrize("mode", ["after_stash", "after_persist"])
def test_a_crash_before_the_replacement_commits_leaves_the_old_version_whole(
    docs_dir: Path, tmp_path: Path, mode: str
) -> None:
    before = _run(docs_dir, tmp_path, mode)

    settled = dr._finish_interrupted_rewrites(docs_dir)
    _consistent(docs_dir, "ORIGINAL")
    assert settled == (1, 0, 0), f"recovery read the crash as {settled}"
    assert before == {"marker": False, "summary_landed": False}, (
        "the old summary committed beside the replacement's stash"
    )
    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 0, 0)


def test_a_crash_after_the_replacement_commits_keeps_the_new_version(
    docs_dir: Path, tmp_path: Path
) -> None:
    _run(docs_dir, tmp_path, "after_commit")

    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 1, 0)
    _consistent(docs_dir, "REPLACEMENT")


def test_a_replacement_that_fails_without_a_crash_stays_consistent(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(dr.app, raise_server_exceptions=False)
    captured: list = []
    monkeypatch.setattr(dr, "_generate_deferred_summary", lambda *a, **k: captured.append(a))
    assert client.post(
        "/parse", files={"file": ("doc.html", b"<p>ORIGINAL</p>", "text/html")},
        data={"doc_id": DOC, "summary_mode": "defer"},
    ).status_code == 200
    a = captured[0]
    real_persist = dr._persist_source_file

    def persist_then_summary(*args, **kwargs):
        record = real_persist(*args, **kwargs)
        dr.write_output(a[0], a[1], "ORIGINAL summary completed", "b", a[2], tags=a[4],
                        metadata=a[5], source_record=a[6], content_type=a[7],
                        source="upload", guard_stale_generation=True)
        return record

    monkeypatch.setattr(dr, "_persist_source_file", persist_then_summary)
    monkeypatch.setattr(
        dr, "_update_doc_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    response = client.post(
        "/parse", files={"file": ("doc.html", b"<p>REPLACEMENT</p>", "text/html")},
        data={"doc_id": DOC, "summary_mode": "off", "replace": "true"},
    )
    assert response.status_code == 500
    monkeypatch.undo()
    _consistent(docs_dir, "ORIGINAL")
    assert dr._finish_interrupted_rewrites(docs_dir) == (0, 0, 0)


def test_a_guarded_write_beside_a_stash_does_nothing(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And it is the stash that stops it: the same write lands once it is gone."""
    client = TestClient(dr.app, raise_server_exceptions=False)
    captured: list = []
    monkeypatch.setattr(dr, "_generate_deferred_summary", lambda *a, **k: captured.append(a))
    assert client.post(
        "/parse", files={"file": ("doc.html", b"<p>ORIGINAL</p>", "text/html")},
        data={"doc_id": DOC, "summary_mode": "defer"},
    ).status_code == 200
    a = captured[0]
    doc = docs_dir / "General" / DOC

    def late_summary():
        return dr.write_output(
            a[0], a[1], "late summary", "late brief", a[2], tags=a[4], metadata=a[5],
            source_record=a[6], content_type=a[7], source="upload", guard_stale_generation=True,
        )

    (doc / ".rollback-source").mkdir()
    digest_before = (doc / "digest.md").read_bytes()
    assert late_summary() is None
    assert (doc / "digest.md").read_bytes() == digest_before
    assert not (doc / ".rewrite-committed").exists()
    assert not (doc / ".rollback").exists()

    (doc / ".rollback-source").rmdir()
    late_summary()
    assert "late summary" in (doc / "digest.md").read_text(encoding="utf-8")

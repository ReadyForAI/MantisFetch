"""A web summary may only write back to the document it was started for.

`_defer_web_summary` used to check one thing before writing: that
`manifest.json` still existed. The docstring said why that was enough — "web
captures are not re-parsed under the same id" — and that assumption is simply
not true. `WEB-1` is a valid doc_id for `/doc/parse`, `replace=true` is a valid
argument, and the replacement returns 200. The old summary then wrote its
digest over the new document's, so the body came from one source and the
summary from another. An agent reading only the digest cannot tell.

Docreader has had the answer to this since #212/#168: a generation token,
checked under a lock every writer takes. The web path now uses the same two.
"""

import json
import threading

import pytest


@pytest.fixture()
def docs(monkeypatch, tmp_path):
    import mantisfetch_common.storage as cs

    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


def _capture(docs_dir, doc_id="WEB-1", digest="OLD WEB DIGEST"):
    import mantisfetch_browser as web

    web._persist_web_capture(
        doc_id,
        "https://example.com/page",
        "Old Web",
        [{"h": "Old Web", "t": "the original captured body", "sid": "s_001"}],
        digest,
        [],
        "hash-web",
        docs_dir,
        summary_mode="defer",
    )
    return docs_dir / "General" / doc_id


def test_a_capture_records_a_generation(docs) -> None:
    """Nothing can be checked against a token the capture never wrote."""
    doc_dir = _capture(docs)
    manifest = json.loads((doc_dir / "manifest.json").read_text())
    assert manifest["provenance"]["generation"]


def test_a_second_capture_of_the_same_id_changes_it(docs) -> None:
    first = json.loads((_capture(docs) / "manifest.json").read_text())
    second = json.loads((_capture(docs) / "manifest.json").read_text())
    assert first["provenance"]["generation"] != second["provenance"]["generation"]


def test_an_old_summary_does_not_write_over_the_document_that_replaced_it(
    client, docs, monkeypatch
) -> None:
    """The report's repro, made deterministic: the summary is held inside the
    LLM call while a real /doc/parse replaces the document under it."""
    import mantisfetch_browser as web
    import mantisfetch_docreader as dr

    doc_dir = _capture(docs)
    sections = [{"h": "Old Web", "t": "the original captured body", "sid": "s_001"}]

    in_llm = threading.Event()
    release = threading.Event()

    def _slow_summaries(parsed, concurrency, *a, **kw):
        in_llm.set()
        release.wait(10)
        return "OLD WEB LLM DIGEST", "OLD WEB LLM BRIEF", {}

    monkeypatch.setattr(dr, "generate_summaries", _slow_summaries)
    worker = threading.Thread(
        target=web._defer_web_summary,
        args=(
            "WEB-1",
            sections,
            docs,
            "General",
            "Old Web",
            "https://example.com/page",
            web._web_doc_generation(doc_dir),
        ),
    )
    worker.start()
    assert in_llm.wait(10), "the summary never reached the LLM call"

    replaced = client.post(
        "/doc/parse",
        files={"file": ("replacement.txt", b"brand new body worth keeping.", "text/plain")},
        data={
            "summary_mode": "off",
            "generate_summary": "false",
            "doc_id": "WEB-1",
            "replace": "true",
        },
    )
    assert replaced.status_code == 200, replaced.text

    release.set()
    worker.join(10)
    assert not worker.is_alive()

    manifest = json.loads((doc_dir / "manifest.json").read_text())
    assert manifest["file_type"] == "txt", "the replacement is the document now"
    digest = (doc_dir / "digest.md").read_text() if (doc_dir / "digest.md").exists() else ""
    assert "OLD WEB LLM DIGEST" not in digest, "the old summary wrote over the new document"
    assert "OLD WEB LLM BRIEF" not in (
        (doc_dir / "brief.md").read_text() if (doc_dir / "brief.md").exists() else ""
    )


def test_an_old_summary_does_not_resurrect_a_deleted_and_recreated_id(docs, monkeypatch) -> None:
    """Same shape without the parse route: delete, capture the id again, and the
    first summary must not land on the second document."""
    import mantisfetch_browser as web
    import mantisfetch_docreader as dr
    from mantisfetch_docreader.storage import _delete_doc

    doc_dir = _capture(docs)
    sections = [{"h": "Old Web", "t": "the original captured body", "sid": "s_001"}]

    in_llm = threading.Event()
    release = threading.Event()

    def _slow_summaries(parsed, concurrency, *a, **kw):
        in_llm.set()
        release.wait(10)
        return "STALE DIGEST", "STALE BRIEF", {}

    monkeypatch.setattr(dr, "generate_summaries", _slow_summaries)
    worker = threading.Thread(
        target=web._defer_web_summary,
        args=(
            "WEB-1",
            sections,
            docs,
            "General",
            "Old Web",
            "https://example.com/page",
            web._web_doc_generation(doc_dir),
        ),
    )
    worker.start()
    assert in_llm.wait(10)

    _delete_doc(docs, "WEB-1")
    _capture(docs, digest="SECOND CAPTURE")

    release.set()
    worker.join(10)
    assert "STALE DIGEST" not in (doc_dir / "digest.md").read_text()


def test_a_summary_for_the_document_it_started_on_still_completes(docs, monkeypatch) -> None:
    """The guard must not break the ordinary path."""
    import mantisfetch_browser as web
    import mantisfetch_docreader as dr

    doc_dir = _capture(docs)
    monkeypatch.setattr(
        dr, "generate_summaries", lambda *a, **kw: ("FRESH DIGEST", "FRESH BRIEF", {})
    )

    web._defer_web_summary(
        "WEB-1",
        [{"h": "Old Web", "t": "the original captured body", "sid": "s_001"}],
        docs,
        "General",
        "Old Web",
        "https://example.com/page",
        web._web_doc_generation(doc_dir),
    )

    assert "FRESH DIGEST" in (doc_dir / "digest.md").read_text()
    assert web._read_web_summary_status(doc_dir) == "completed"


# ── the generation is bound when the summary is asked for, not when it runs ──────
def _spin_until(predicate, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        time.sleep(0.01)


@pytest.fixture()
def one_busy_slot(monkeypatch):
    """The summary slot, taken. A fresh semaphore rather than the module's, so
    nothing leaks into the next test if this one fails half-way."""
    import mantisfetch_browser as web

    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(web, "_web_summary_sem", sem)
    sem.acquire()
    return sem


def test_a_summary_waiting_for_its_slot_does_not_claim_the_document_that_replaced_it(
    docs, monkeypatch, one_busy_slot
) -> None:
    """The review's R02: the worker used to read the generation *after* getting a
    slot, so a capture deleted and recaptured under the same id while it waited
    handed the worker the new generation — every later check then agreed, and
    the new document got a summary of the old body."""
    import mantisfetch_browser as web
    import mantisfetch_docreader as dr
    from mantisfetch_docreader.storage import _delete_doc

    doc_dir = _capture(docs)
    generation = web._web_doc_generation(doc_dir)
    old_sections = [{"h": "Old Web", "t": "the original captured body", "sid": "s_001"}]
    summarized: list[str] = []

    def _summaries(parsed, *a, **kw):
        summarized.append(parsed.sections[0].text)
        return "OLD BODY DIGEST", "OLD BODY BRIEF", {}

    monkeypatch.setattr(dr, "generate_summaries", _summaries)
    worker = threading.Thread(
        target=web._defer_web_summary,
        args=(
            "WEB-1",
            old_sections,
            docs,
            "General",
            "Old Web",
            "https://example.com/page",
            generation,
        ),
    )
    worker.start()
    # The precondition for the race: parked at the slot, having read nothing.
    _spin_until(lambda: web._web_summary_waiting == 1)

    _delete_doc(docs, "WEB-1")
    _capture(docs, digest="SECOND CAPTURE")
    assert web._web_doc_generation(doc_dir) != generation

    one_busy_slot.release()
    worker.join(10)
    assert not worker.is_alive()

    assert summarized == [], "the old sections were summarized for the new document"
    assert "OLD BODY DIGEST" not in (doc_dir / "digest.md").read_text()
    assert web._read_web_summary_status(doc_dir) == "pending", (
        "the new capture's own summary status was taken over"
    )


def test_a_refused_summary_does_not_mark_the_recapture_that_replaced_it(
    docs, monkeypatch, one_busy_slot
) -> None:
    """Queue full, and the id was recaptured before the refusal is written. A web
    capture replacing a web capture passes the file_type check, so only the
    bound generation stands between the refusal and the new document."""
    import mantisfetch_browser as web
    from mantisfetch_docreader.storage import _delete_doc

    doc_dir = _capture(docs)
    generation = web._web_doc_generation(doc_dir)
    _delete_doc(docs, "WEB-1")
    _capture(docs, digest="SECOND CAPTURE")

    monkeypatch.setattr(web, "_WEB_SUMMARY_MAX_QUEUED", 0)
    web._defer_web_summary(
        "WEB-1",
        [{"h": "Old Web", "t": "the original captured body", "sid": "s_001"}],
        docs,
        "General",
        "Old Web",
        "https://example.com/page",
        generation,
    )

    assert web._read_web_summary_status(doc_dir) == "pending"


def _index_entry(docs_dir, doc_id="WEB-1"):
    import mantisfetch_common.doc_index_store as dis

    return next(e for e in dis.list_documents(docs_dir) if e["id"] == doc_id)


class _RecordedThread:
    started: list[tuple] = []

    def __init__(self, target, args, daemon, name):
        self.args = args

    def start(self):
        _RecordedThread.started.append(self.args)


def test_a_cache_hit_hands_its_worker_the_generation_it_claimed(docs, monkeypatch) -> None:
    import types

    import mantisfetch_browser as web

    doc_dir = _capture(docs)
    web._set_web_summary_status(doc_dir, "failed")
    _RecordedThread.started = []
    monkeypatch.setattr(web, "threading", types.SimpleNamespace(Thread=_RecordedThread))

    assert web._resolve_cached_summary(_index_entry(docs), docs, "General", "defer") == "pending"
    assert len(_RecordedThread.started) == 1
    assert _RecordedThread.started[0][-1] == web._web_doc_generation(doc_dir)


def test_a_cache_hit_does_not_claim_a_document_that_is_no_longer_a_capture(
    docs, monkeypatch
) -> None:
    import types

    import mantisfetch_browser as web

    doc_dir = _capture(docs)
    entry = _index_entry(docs)
    manifest = json.loads((doc_dir / "manifest.json").read_text())
    manifest["file_type"] = "txt"
    manifest["parse_metadata"] = {}
    (doc_dir / "manifest.json").write_text(json.dumps(manifest))
    _RecordedThread.started = []
    monkeypatch.setattr(web, "threading", types.SimpleNamespace(Thread=_RecordedThread))

    web._resolve_cached_summary(entry, docs, "General", "defer")

    assert _RecordedThread.started == []
    after = json.loads((doc_dir / "manifest.json").read_text())
    assert after.get("parse_metadata") == {}, "the claim wrote onto a document that is not ours"


def test_a_cache_hit_claim_waits_for_a_writer_holding_the_document(docs, monkeypatch) -> None:
    """The claim rewrites the whole manifest. Without the writer lock it could
    land in the middle of a replacement and put the old capture's manifest back."""
    import types

    import mantisfetch_browser as web
    from mantisfetch_docreader import _document_writer_lock

    doc_dir = _capture(docs)
    web._set_web_summary_status(doc_dir, "failed")
    _RecordedThread.started = []
    monkeypatch.setattr(web, "threading", types.SimpleNamespace(Thread=_RecordedThread))
    entry = _index_entry(docs)

    done = threading.Event()

    def _claim():
        web._resolve_cached_summary(entry, docs, "General", "defer")
        done.set()

    with _document_writer_lock(docs, "WEB-1"):
        claimant = threading.Thread(target=_claim)
        claimant.start()
        assert not done.wait(0.3), "the claim ran while another writer held the document"
    assert done.wait(5)
    claimant.join(5)
    assert len(_RecordedThread.started) == 1

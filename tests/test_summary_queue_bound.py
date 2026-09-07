"""The summary queue has a bottom, and a document that misses it says so.

One MantisFetch serves several NodalOS instances and every agent on them, and
every MCP ingest defers its summary — the tool always declares a budget, and a
declared budget defers. So arrivals are a fan-in while the drain is this one
process making a section-by-section LLM call at a time
(`DEFERRED_SUMMARY_MAX_CONCURRENT`, default 1). Nothing bounded the queue
between them: each waiting document was a thread holding its whole
ParsedDocument, and a stalled provider took the drain rate to zero without
slowing arrivals at all.

Past the bound the caller is told. The extraction is on disk either way — it is
the summary that did not happen, and `not_queued` is a state a caller can
retry, unlike a `pending` nobody is working on.
"""

import threading

import pytest


@pytest.fixture()
def docs(monkeypatch, tmp_path):
    import mantisfetch_common.storage as cs

    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


def _parsed(dr, doc_id):
    return dr.ParsedDocument(
        filename=f"{doc_id}.txt",
        file_type="txt",
        total_pages=1,
        pages=[],
        sections=[
            dr.Section(
                index=1,
                title="T",
                level=1,
                text="body worth summarising",
                page_range="1",
                sid="s_001",
            )
        ],
    )


def _seed(dr, docs_dir, doc_id):
    """What the parse handler has already written before it defers a summary:
    the extraction, with the summary marked pending. Without it the deferred
    write is skipped as "the document was deleted while its summary ran"."""
    parsed = _parsed(dr, doc_id)
    dr._set_summary_metadata(parsed, mode="defer", status="pending")
    dr.write_output_extract_only(
        doc_id,
        parsed,
        docs_dir,
        tags=[],
        source="upload",
        metadata={},
        source_record={},
        content_type="General",
    )
    return parsed


def _status(docs_dir, doc_id):
    import json

    manifest = json.loads((docs_dir / "General" / doc_id / "manifest.json").read_text())
    return manifest.get("parse_metadata", {}).get("summary", {}).get("status")


def test_a_document_past_the_bound_is_told_instead_of_queued(docs, monkeypatch) -> None:
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "DEFERRED_SUMMARY_MAX_QUEUED", 2)
    release = threading.Event()
    started = threading.Semaphore(0)

    def _slow(parsed, concurrency, *a, **kw):
        started.release()
        release.wait(10)
        return "D", "B", []

    monkeypatch.setattr(dr, "generate_summaries", _slow)

    # One takes the only running slot; two more fill the queue; the fourth is refused.
    threads = []
    for i in range(4):
        doc_id = f"DOC-{i:03d}"
        t = threading.Thread(
            target=dr._generate_deferred_summary,
            args=(doc_id, _seed(dr, docs, doc_id), docs, 1, [], {}, {}, "General"),
            # daemon so a regression that puts the bound back to unbounded fails
            # this test instead of hanging the run behind a thread that will
            # never get a slot.
            daemon=True,
        )
        t.start()
        threads.append((doc_id, t))
        if i == 0:
            assert started.acquire(timeout=10), "the first summary never started"

    refused = threads[-1]
    refused[1].join(10)
    assert not refused[1].is_alive(), "the fourth summary waited instead of being refused"
    assert _status(docs, refused[0]) == "not_queued"
    assert (docs / "General" / refused[0] / "full.md").exists(), (
        "the extraction has to land even when the summary does not"
    )

    release.set()
    for doc_id, t in threads[:-1]:
        t.join(15)
        assert not t.is_alive(), f"{doc_id} never finished"
        assert _status(docs, doc_id) == "completed"


def test_the_queue_drains_and_accepts_again(docs, monkeypatch) -> None:
    """A full queue is a moment, not a state: once it drains, the next document
    is queued normally."""
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "DEFERRED_SUMMARY_MAX_QUEUED", 1)
    monkeypatch.setattr(dr, "generate_summaries", lambda *a, **kw: ("D", "B", []))

    dr._generate_deferred_summary(
        "DOC-900", _seed(dr, docs, "DOC-900"), docs, 1, [], {}, {}, "General"
    )
    assert _status(docs, "DOC-900") == "completed"
    assert dr._deferred_summary_waiting == 0, "the waiter count leaked"


def test_the_slot_is_not_leaked_when_the_queue_refuses(docs, monkeypatch) -> None:
    """A refusal must not consume the slot it did not get."""
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "DEFERRED_SUMMARY_MAX_QUEUED", 0)
    monkeypatch.setattr(dr, "generate_summaries", lambda *a, **kw: ("D", "B", []))

    dr._deferred_summary_sem.acquire()
    try:
        dr._generate_deferred_summary(
            "DOC-901", _seed(dr, docs, "DOC-901"), docs, 1, [], {}, {}, "General"
        )
        assert _status(docs, "DOC-901") == "not_queued"
    finally:
        dr._deferred_summary_sem.release()

    monkeypatch.setattr(dr, "DEFERRED_SUMMARY_MAX_QUEUED", 4)
    dr._generate_deferred_summary(
        "DOC-902", _seed(dr, docs, "DOC-902"), docs, 1, [], {}, {}, "General"
    )
    assert _status(docs, "DOC-902") == "completed"


def test_a_not_queued_document_can_be_retried(client, docs, monkeypatch) -> None:
    """`not_queued` has to be a state the retry endpoint accepts, or the caller
    is told to retry something that refuses to be retried."""
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "DEFERRED_SUMMARY_MAX_QUEUED", 0)
    monkeypatch.setattr(dr, "generate_summaries", lambda *a, **kw: ("D", "B", []))

    dr._deferred_summary_sem.acquire()
    try:
        dr._generate_deferred_summary(
            "DOC-903", _seed(dr, docs, "DOC-903"), docs, 1, [], {}, {}, "General"
        )
    finally:
        dr._deferred_summary_sem.release()
    assert _status(docs, "DOC-903") == "not_queued"

    resp = client.post("/doc/library/DOC-903/summary")
    assert resp.status_code == 200, resp.text


# ── what a restart leaves behind ─────────────────────────────────────────────────
def test_a_summary_left_running_by_a_restart_becomes_pending(docs) -> None:
    """A deferred summary lives in a daemon thread, so a restart takes every
    in-flight one with it while `running` stays on disk. Nothing swept it: the
    document claimed a summary was in progress for as long as it existed, and an
    agent polling the status face could not tell that from one really running."""
    import json

    import mantisfetch_docreader as dr

    _seed(dr, docs, "DOC-910")
    manifest_path = docs / "General" / "DOC-910" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["parse_metadata"]["summary"]["status"] = "running"
    manifest_path.write_text(json.dumps(manifest))
    from mantisfetch_docreader.storage import _update_doc_index

    _update_doc_index(
        docs,
        {
            "doc_id": "DOC-910",
            "filename": "x.txt",
            "file_type": "txt",
            "total_pages": 1,
            "section_count": 1,
            "created_at": "2026-09-07T00:00:00Z",
            "storage_path": "General/DOC-910",
            "parse_metadata": {"summary": {"mode": "defer", "status": "running"}},
        },
        "d",
    )

    assert dr._reset_interrupted_summaries(docs) == 1
    assert _status(docs, "DOC-910") == "pending"


def test_the_sweep_leaves_finished_summaries_alone(docs) -> None:
    import json

    import mantisfetch_docreader as dr

    _seed(dr, docs, "DOC-911")
    manifest_path = docs / "General" / "DOC-911" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["parse_metadata"]["summary"]["status"] = "completed"
    manifest_path.write_text(json.dumps(manifest))

    assert dr._reset_interrupted_summaries(docs) == 0
    assert _status(docs, "DOC-911") == "completed"


def test_the_sweep_does_not_re_enqueue(docs, monkeypatch) -> None:
    """With several NodalOS instances behind one MantisFetch, a restart that
    re-queued everything it found would refill the bounded queue at the worst
    possible moment. The sweep only tells the truth about the state."""
    import json

    import mantisfetch_docreader as dr

    called = {"n": 0}
    monkeypatch.setattr(
        dr,
        "generate_summaries",
        lambda *a, **kw: (called.__setitem__("n", called["n"] + 1), ("D", "B", []))[1],
    )
    _seed(dr, docs, "DOC-912")
    manifest_path = docs / "General" / "DOC-912" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["parse_metadata"]["summary"]["status"] = "running"
    manifest_path.write_text(json.dumps(manifest))

    dr._reset_interrupted_summaries(docs)
    assert called["n"] == 0

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
        args=("WEB-1", sections, docs, "General", "Old Web", "https://example.com/page"),
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
        args=("WEB-1", sections, docs, "General", "Old Web", "https://example.com/page"),
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
    )

    assert "FRESH DIGEST" in (doc_dir / "digest.md").read_text()
    assert web._read_web_summary_status(doc_dir) == "completed"

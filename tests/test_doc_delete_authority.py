"""SharedSpecs IRP 20260908 as the library implements it.

The library is a working set shared by several NodalOS instances, not a
knowledge base, so the permission model is ownership + audit + retention, with
the delete button taken off the LLM face — not an authorisation gate:

* D1  ``mantisfetch.doc_delete`` leaves the MCP tool face; REST DELETE stays.
* D3  every write records who wrote it, off two transport headers, by a fixed
      fill rule (ruling ①); recorded verbatim, never checked against anything
      (ruling ②) — so it is an audit field and a missing header does not refuse
      the write.
* D4  the library reclaims its own documents by age, through the same locked
      path as DELETE, and only when a deployment turns it on.
* D6  every delete says who asked and who had written the document.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mantisfetch_common.actor import actor_from_headers, actor_label, forwardable_headers

HUMAN = {
    "X-RFAI-Actor-ID": "human:alice@example.com",
    "X-NodalOS-Caller-Agent-ID": "councilor",
}
HTML = b"<h1>T</h1><p>Body worth keeping around for a test.</p>"


# ── ruling ①: the fill rule ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, (None, None)),
        ({"X-RFAI-Actor-ID": "human:a@x"}, ("human:a@x", None)),
        (HUMAN, ("human:alice@example.com", "agent:councilor")),
        # A dispatched Worker: NodalOS strips the actor at the dispatch boundary
        # and only the caller alias arrives. This is the common case, not a gap.
        ({"X-NodalOS-Caller-Agent-ID": "advisor-finance"}, (None, "agent:advisor-finance")),
        ({"X-RFAI-Actor-ID": "service:harness-server"}, (None, "service:harness-server")),
        # D1 reserves agent: on the actor header for a later rail; it is not human,
        # so it is "via", never "by".
        ({"X-RFAI-Actor-ID": "agent:some-alias"}, (None, "agent:some-alias")),
        ({"x-rfai-actor-id": "human:lower@x"}, ("human:lower@x", None)),
        ({"X-RFAI-Actor-ID": "  human:padded@x  "}, ("human:padded@x", None)),
        ({"X-RFAI-Actor-ID": "human:"}, (None, None)),
        ({"X-RFAI-Actor-ID": "root:alice"}, (None, None)),
        ({"X-RFAI-Actor-ID": "alice"}, (None, None)),
        ({"X-NodalOS-Caller-Agent-ID": "   "}, (None, None)),
    ],
)
def test_fill_rule(headers, expected) -> None:
    assert actor_from_headers(headers) == expected


def test_a_malformed_actor_is_dropped_and_said_so(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="mantisfetch_common.actor"):
        assert actor_from_headers({"X-RFAI-Actor-ID": "nobody"}) == (None, None)
    assert "malformed" in caplog.text


def test_only_the_two_identity_headers_travel() -> None:
    """The MCP server passes these through to the sub-apps; the caller's own
    credentials must never ride along on that hop."""
    out = forwardable_headers({**HUMAN, "Authorization": "Bearer s3cret", "Host": "x"})
    assert out == HUMAN


def test_actor_label_names_the_caller_or_says_unknown() -> None:
    assert actor_label(None) == "unknown"
    assert actor_label((None, None)) == "unknown"
    assert actor_label(("human:a", "agent:b")) == "human:a"
    assert actor_label((None, "service:harness-server")) == "service:harness-server"


# ── D3: every write path records it ─────────────────────────────────────────────


def _parse(client, headers=None, **data):
    return client.post(
        "/doc/parse",
        files={"file": ("p.html", HTML, "text/html")},
        data={"summary_mode": "off", "generate_summary": "false", **data},
        headers=headers or {},
    )


def _provenance(doc_id: str) -> dict:
    import mantisfetch_docreader as d

    from mantisfetch_common.storage import _get_docs_dir

    doc_dir = d._resolve_doc_dir(_get_docs_dir(), doc_id)
    return json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))["provenance"]


def test_parse_records_who_wrote_it(client) -> None:
    resp = _parse(client, HUMAN)
    assert resp.status_code == 200, resp.text
    prov = _provenance(resp.json()["doc_id"])
    assert prov["created_by"] == "human:alice@example.com"
    assert prov["created_via"] == "agent:councilor"


def test_parse_without_headers_still_writes_and_records_nothing(client) -> None:
    """Ownership is an audit field, not a gate: no header, no refusal."""
    resp = _parse(client)
    assert resp.status_code == 200, resp.text
    prov = _provenance(resp.json()["doc_id"])
    assert (prov["created_by"], prov["created_via"]) == (None, None)


def test_store_only_records_who_wrote_it(client) -> None:
    resp = client.post(
        "/doc/parse",
        files={"file": ("notes.md", b"# hello\n", "application/octet-stream")},
        data={"store_only": "true"},
        headers={"X-RFAI-Actor-ID": "service:harness-server"},
    )
    assert resp.status_code == 200, resp.text
    prov = _provenance(resp.json()["doc_id"])
    assert prov["created_by"] is None
    assert prov["created_via"] == "service:harness-server"


def test_web_capture_records_who_wrote_it(tmp_path: Path) -> None:
    import mantisfetch_browser as lb

    lb._persist_web_capture(
        doc_id="WEB-050",
        url="https://example.com",
        title="Ex",
        sections=[{"heading": "H", "text": "body", "level": 1}],
        digest="d",
        tags=[],
        content_hash="h",
        docs_dir=tmp_path,
        actor=("human:alice@example.com", "agent:councilor"),
    )
    manifest = next(tmp_path.rglob("manifest.json"))
    prov = json.loads(manifest.read_text(encoding="utf-8"))["provenance"]
    assert prov["created_by"] == "human:alice@example.com"
    assert prov["created_via"] == "agent:councilor"


def test_web_endpoint_reads_the_request_and_hands_it_down(monkeypatch) -> None:
    import mantisfetch_browser as lb

    seen: dict = {}

    async def fake_impl(req, *, url_ttl_hours=None, actor=None):
        seen["actor"] = actor
        return "response"

    monkeypatch.setattr(lb, "_capture_impl", fake_impl)
    req = lb.CaptureRequest(url="https://example.com")
    assert asyncio.run(lb.capture(req, SimpleNamespace(headers=HUMAN))) == "response"
    assert seen["actor"] == ("human:alice@example.com", "agent:councilor")


def test_a_rewrite_without_an_actor_keeps_the_recorded_one(tmp_path: Path) -> None:
    """The deferred-summary write-back re-runs the same writers with no actor
    in hand. It must carry the recorded owner forward, not blank it."""
    import mantisfetch_docreader as d

    (tmp_path / "manifest.json").write_text(
        json.dumps({"provenance": {"created_by": "human:a@x", "created_via": "agent:c"}}),
        encoding="utf-8",
    )
    assert d._actor_provenance(tmp_path, None) == {
        "created_by": "human:a@x",
        "created_via": "agent:c",
    }
    # A write that did arrive over the wire says what it was told, even if less.
    assert d._actor_provenance(tmp_path, (None, "service:aulo")) == {
        "created_by": None,
        "created_via": "service:aulo",
    }
    assert d._actor_provenance(tmp_path / "nowhere", None) == {
        "created_by": None,
        "created_via": None,
    }


# ── the MCP front-end forwards exactly the two headers ───────────────────────────


def _fake_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("POST", "http://mantisfetch"))


def test_mcp_doc_parse_forwards_the_identity_and_not_the_credentials(monkeypatch) -> None:
    import mantisfetch_mcp as mm

    seen: dict = {}

    async def fake_post(path, **kwargs):
        seen["path"], seen["headers"] = path, kwargs.get("headers")
        return _fake_response({"doc_id": "DOC-1"})

    monkeypatch.setattr(mm._doc_client, "post", fake_post)
    ctx = SimpleNamespace(headers={**HUMAN, "Authorization": "Bearer s3cret"})
    asyncio.run(
        mm.doc_parse(content_b64=base64.b64encode(b"# hi").decode(), filename="n.md", ctx=ctx)
    )
    assert seen["path"] == "/parse"
    assert seen["headers"] == HUMAN


def test_mcp_web_capture_forwards_the_identity_and_not_the_credentials(monkeypatch) -> None:
    import mantisfetch_mcp as mm

    seen: dict = {}

    async def fake_post(path, **kwargs):
        seen["path"], seen["headers"] = path, kwargs.get("headers")
        return _fake_response({"doc_id": "WEB-1", "digest": "d", "title": "t", "url": "u"})

    monkeypatch.setattr(mm._web_client, "post", fake_post)
    ctx = SimpleNamespace(headers={**HUMAN, "Authorization": "Bearer s3cret"})
    asyncio.run(mm.web_capture(url="https://example.com", ctx=ctx))
    assert seen["path"] == "/capture"
    assert seen["headers"] == HUMAN


def test_mcp_tool_called_without_a_context_sends_no_identity(monkeypatch) -> None:
    import mantisfetch_mcp as mm

    seen: dict = {}

    async def fake_post(path, **kwargs):
        seen["headers"] = kwargs.get("headers")
        return _fake_response({"doc_id": "DOC-1"})

    monkeypatch.setattr(mm._doc_client, "post", fake_post)
    asyncio.run(mm.doc_parse(content_b64=base64.b64encode(b"# hi").decode(), filename="n.md"))
    assert seen["headers"] == {}


def test_the_context_parameter_is_not_part_of_any_tool_schema() -> None:
    import mantisfetch_mcp as mm

    for tool in asyncio.run(mm.mcp.list_tools()):
        assert "ctx" not in (tool.input_schema.get("properties") or {}), tool.name


# ── D1: delete is off the LLM face; REST keeps its contract ─────────────────────


def test_doc_delete_is_not_on_the_llm_face(client) -> None:
    import mantisfetch_mcp as mm

    names = {t.name for t in asyncio.run(mm.mcp.list_tools())}
    assert "doc_delete" not in names
    assert not hasattr(mm, "doc_delete")
    # The REST contract (GC IRP D2) is untouched: idempotent 200 on an unknown id.
    resp = client.delete("/doc/library/DOC-999")
    assert resp.status_code == 200
    assert resp.json() == {"doc_id": "DOC-999", "deleted": False}


# ── D6: every delete says who asked and who had written it ──────────────────────


def _audit_line(caplog) -> str:
    return next(
        r.getMessage() for r in caplog.records if r.getMessage().startswith("library_delete")
    )


def test_delete_logs_who_asked_and_who_wrote(client, caplog) -> None:
    doc_id = _parse(client, HUMAN).json()["doc_id"]
    with caplog.at_level(logging.INFO, logger="mantisfetch_docreader"):
        resp = client.delete(
            f"/doc/library/{doc_id}", headers={"X-RFAI-Actor-ID": "service:harness-server"}
        )
    assert resp.json()["deleted"] is True
    line = _audit_line(caplog)
    assert f"doc_id={doc_id}" in line
    assert "trigger=rest" in line
    assert "actor=service:harness-server" in line
    assert "created_by=human:alice@example.com" in line
    assert "created_via=agent:councilor" in line
    assert "deleted=True" in line


def test_delete_without_headers_logs_unknown(client, caplog) -> None:
    doc_id = _parse(client).json()["doc_id"]
    with caplog.at_level(logging.INFO, logger="mantisfetch_docreader"):
        client.delete(f"/doc/library/{doc_id}")
    line = _audit_line(caplog)
    assert "actor=unknown" in line
    assert "created_by=None" in line


# ── D4: retention ────────────────────────────────────────────────────────────────


def test_expired_selection_keeps_anything_it_cannot_date(monkeypatch) -> None:
    import mantisfetch_docreader as d

    from mantisfetch_common import doc_index_store as dis

    rows = [
        {"id": "OLD", "created_at": "2020-01-01T00:00:00Z"},
        {"id": "NEW", "created_at": "2999-01-01T00:00:00Z"},
        {"id": "BAD", "created_at": "yesterday"},
        {"id": "UNDATED"},
        {"created_at": "2020-01-01T00:00:00Z"},  # no id: nothing to delete by
    ]
    monkeypatch.setattr(dis, "list_documents", lambda docs_dir: rows)
    assert d._expired_doc_ids(Path("."), datetime.now(UTC)) == ["OLD"]


def test_sweep_deletes_through_the_locked_path_and_audits(client, caplog) -> None:
    import mantisfetch_docreader as d

    from mantisfetch_common.storage import _get_docs_dir

    doc_id = _parse(client, HUMAN).json()["doc_id"]
    docs_dir = _get_docs_dir()
    with caplog.at_level(logging.INFO, logger="mantisfetch_docreader"):
        # A negative age puts the cutoff in the future: everything is expired.
        deleted = asyncio.run(d._sweep_expired_documents(docs_dir, days=-1))
    assert deleted == 1
    assert client.get(f"/doc/library/{doc_id}/manifest").status_code == 404
    assert client.get("/doc/library/search").json()["total"] == 0
    line = _audit_line(caplog)
    assert "trigger=retention" in line
    assert "actor=unknown" in line
    assert "created_by=human:alice@example.com" in line


def test_sweep_keeps_young_documents(client) -> None:
    import mantisfetch_docreader as d

    from mantisfetch_common.storage import _get_docs_dir

    doc_id = _parse(client).json()["doc_id"]
    assert asyncio.run(d._sweep_expired_documents(_get_docs_dir(), days=3650)) == 0
    assert client.get(f"/doc/library/{doc_id}/manifest").status_code == 200


def test_sweep_rechecks_age_under_the_lock(client, caplog, monkeypatch) -> None:
    """Codex review, P1. The sweep picks candidates from the index without a
    lock. Between that read and the delete, a /parse with replace=true can put a
    new document under the same id with a fresh created_at — and the caller has
    already been told 200. The age has to be re-read under the per-doc lock the
    parse itself holds while writing, so the sweep sees the old document or the
    finished new one, never the gap. Simulated by handing the sweep a stale
    selection: a document the index says is expired but the disk says is new."""
    import mantisfetch_docreader as d

    from mantisfetch_common.storage import _get_docs_dir

    doc_id = _parse(client, HUMAN).json()["doc_id"]
    docs_dir = _get_docs_dir()
    monkeypatch.setattr(d, "_expired_doc_ids", lambda docs_dir, cutoff: [doc_id])

    with caplog.at_level(logging.INFO, logger="mantisfetch_docreader"):
        assert asyncio.run(d._sweep_expired_documents(docs_dir, days=3650)) == 0
    assert client.get(f"/doc/library/{doc_id}/manifest").status_code == 200
    assert any("skipped=no_longer_expired" in r.getMessage() for r in caplog.records)

    # The same stale selection with a cutoff the document really is older than.
    assert asyncio.run(d._sweep_expired_documents(docs_dir, days=-1)) == 1
    assert client.get(f"/doc/library/{doc_id}/manifest").status_code == 404


def test_retention_runs_only_when_a_deployment_turns_it_on(monkeypatch) -> None:
    import mantisfetch_docreader as d

    started: list[int] = []

    async def fake_loop(days: int) -> None:
        started.append(days)
        await asyncio.sleep(3600)

    monkeypatch.setattr(d, "_retention_loop", fake_loop)

    async def boot(days: int) -> None:
        monkeypatch.setattr(d, "LIBRARY_RETENTION_DAYS", days)
        async with d.lifespan(d.app):
            await asyncio.sleep(0)

    asyncio.run(boot(0))
    assert started == []
    asyncio.run(boot(7))
    assert started == [7]

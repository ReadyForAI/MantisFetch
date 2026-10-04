"""Owner-scoped library discovery (SharedSpecs IRP 20261003, decided 2026-10-04).

The switch is off in every other test. These turn it on per request, and the
startup refusal is called directly so the session-scoped server is not rebuilt.
"""

from __future__ import annotations

import ast
import inspect
import json
import os

import pytest
from mantisfetch_docreader.models import ParsedDocument, Section
from starlette.testclient import TestClient

MD = b"# Notes\n\nThe same bytes.\n"
ACTOR = "X-RFAI-Actor-ID"


def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANTISFETCH_OWNER_SCOPED_DISCOVERY", "true")
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "secret")


def _headers(who: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    token = os.environ.get("MANTISFETCH_MCP_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if who:
        headers[ACTOR] = who
    return headers


def _store(
    client: TestClient,
    *,
    who: str | None,
    name: str = "notes.md",
    content: bytes = MD,
    **extra: str,
):
    return client.post(
        "/doc/parse",
        files={"file": (name, content, "application/octet-stream")},
        data={"store_only": "true", **extra},
        headers=_headers(who),
    )


def _ids(client: TestClient, who: str | None, q: str = "notes") -> list[str]:
    response = client.get(
        "/doc/library/search", params={"q": q, "limit": 20}, headers=_headers(who)
    )
    assert response.status_code == 200, response.text
    return [row["doc_id"] for row in response.json()["results"]]


def test_switch_off_still_lists_someone_elses_upload(client: TestClient) -> None:
    stored = _store(client, who="human:alice", name="private.md", content=b"# private\n")
    assert stored.status_code == 200, stored.text
    assert stored.json()["doc_id"] in _ids(client, "human:bob", q="private")


def test_owner_sees_own_shared_and_web_but_not_others(client: TestClient, monkeypatch) -> None:
    _on(monkeypatch)
    own = _store(client, who="human:alice", name="own.md", content=b"# own\n")
    shared = _store(client, who="human:bob", name="shared.md", content=b"# shared\n", shared="true")
    private = _store(client, who="human:bob", name="private.md", content=b"# private\n")
    assert own.status_code == shared.status_code == private.status_code == 200

    from mantisfetch_docreader import _get_docs_dir, _update_doc_index

    _update_doc_index(
        _get_docs_dir(),
        {
            "doc_id": "WEB-900",
            "filename": "public-page",
            "file_type": "web_capture",
            "total_pages": 1,
            "section_count": 1,
            "ocr_page_count": 0,
            "table_count": 0,
            "created_at": "2026-10-04T00:00:00Z",
        },
        "a public web capture",
        source="web_capture",
        created_by=None,
        shared=False,
    )
    empty = _store(client, who=None, name="orphan.md", content=b"# orphan\n")
    assert empty.status_code == 200, empty.text

    visible = _ids(client, "human:alice", q="")
    assert own.json()["doc_id"] in visible
    assert shared.json()["doc_id"] in visible
    assert "WEB-900" in visible
    assert private.json()["doc_id"] not in visible
    assert empty.json()["doc_id"] not in visible

    # No human identity: shared files and web captures only.
    stranger = set(_ids(client, None, q=""))
    assert shared.json()["doc_id"] in stranger
    assert "WEB-900" in stranger
    assert own.json()["doc_id"] not in stranger

    own_row = next(
        row
        for row in client.get(
            "/doc/library/search", params={"q": "own"}, headers=_headers("human:alice")
        ).json()["results"]
        if row["doc_id"] == own.json()["doc_id"]
    )
    assert own_row["created_by"] == "human:alice"
    assert own_row["shared"] is False
    manifest = client.get(
        f"/doc/library/{own.json()['doc_id']}/manifest", headers=_headers("human:alice")
    ).json()
    assert manifest["shared"] is False
    assert manifest["provenance"]["created_by"] == "human:alice"


def test_filter_happens_before_the_page_limit(client: TestClient, monkeypatch) -> None:
    _on(monkeypatch)
    from mantisfetch_docreader import _get_docs_dir, _update_doc_index

    docs = _get_docs_dir()
    meta = {
        "total_pages": 1,
        "section_count": 0,
        "ocr_page_count": 0,
        "table_count": 0,
        "created_at": "2026-10-04T00:00:00Z",
        "file_type": "md",
    }
    for i in range(3):
        _update_doc_index(
            docs,
            {"doc_id": f"B-{i}", "filename": f"needle-{i}.md", **meta},
            "needle in the digest too",
            created_by="human:bob",
            shared=False,
        )
    _update_doc_index(
        docs,
        {"doc_id": "A-ONLY", "filename": "mine.md", **meta},
        "needle only in the digest",
        created_by="human:alice",
        shared=False,
    )
    response = client.get(
        "/doc/library/search",
        params={"q": "needle", "limit": 1},
        headers=_headers("human:alice"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["doc_id"] for row in body["results"]] == ["A-ONLY"]
    assert body["total"] == 1


def test_by_id_read_is_not_filtered(client: TestClient, monkeypatch) -> None:
    _on(monkeypatch)
    stored = _store(client, who="human:alice", name="secret.md", content=b"# secretword\n")
    doc_id = stored.json()["doc_id"]
    manifest = client.get(f"/doc/library/{doc_id}/manifest", headers=_headers("human:bob"))
    assert manifest.status_code == 200
    assert manifest.json()["doc_id"] == doc_id


def test_put_shared_follows_the_owner(client: TestClient, monkeypatch) -> None:
    _on(monkeypatch)
    stored = _store(client, who="human:alice", name="toggle.md", content=b"# toggle\n")
    doc_id = stored.json()["doc_id"]
    denied = client.put(
        f"/doc/library/{doc_id}/shared",
        json={"shared": True},
        headers=_headers("human:bob"),
    )
    assert denied.status_code == 403
    orphan = _store(client, who=None, name="no-owner.md", content=b"# no owner\n")
    assert (
        client.put(
            f"/doc/library/{orphan.json()['doc_id']}/shared",
            json={"shared": True},
            headers=_headers("human:alice"),
        ).status_code
        == 403
    )
    updated = client.put(
        f"/doc/library/{doc_id}/shared",
        json={"shared": True},
        headers=_headers("human:alice"),
    )
    assert updated.status_code == 200, updated.text
    assert doc_id in _ids(client, "human:bob", q="toggle")
    client.put(
        f"/doc/library/{doc_id}/shared",
        json={"shared": False},
        headers=_headers("human:alice"),
    )
    assert doc_id not in _ids(client, "human:bob", q="toggle")


def test_random_id_and_filename_strategy_refused(client: TestClient, monkeypatch) -> None:
    _on(monkeypatch)
    stored = _store(client, who="human:alice", name="fresh.md", content=b"# fresh\n")
    assert stored.status_code == 200, stored.text
    assert stored.json()["doc_id"].startswith("R-")
    explicit = "F-0123456789abcdef0123456789abcdef"
    kept = _store(client, who="human:alice", name="kept.md", content=b"# kept\n", doc_id=explicit)
    assert kept.status_code == 200, kept.text
    assert kept.json()["doc_id"] == explicit
    refused = _store(
        client,
        who="human:alice",
        name="named.md",
        content=b"# named\n",
        id_strategy="source_filename",
    )
    assert refused.status_code == 422


def test_replace_is_owner_only(client: TestClient, monkeypatch) -> None:
    _on(monkeypatch)
    doc_id = "F-abc123"
    first = _store(client, who="human:alice", name="v1.md", content=b"# v1\n", doc_id=doc_id)
    assert first.status_code == 200, first.text
    stolen = _store(
        client,
        who="human:bob",
        name="v2.md",
        content=b"# v2\n",
        doc_id=doc_id,
        replace="true",
    )
    assert stolen.status_code == 403
    replaced = _store(
        client,
        who="human:alice",
        name="v2.md",
        content=b"# v2\n",
        doc_id=doc_id,
        replace="true",
    )
    assert replaced.status_code == 200, replaced.text


def test_dedup_hides_a_private_existing_id(client: TestClient, monkeypatch) -> None:
    _on(monkeypatch)
    first = _store(client, who="human:alice")
    assert first.status_code == 200, first.text
    hidden = _store(client, who="human:bob", name="copy.md")
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["dedup"] == "miss"
    assert hidden.json()["existing_doc_id"] is None
    visible = _store(client, who="human:alice", name="copy2.md")
    assert visible.json()["dedup"] == "hit"
    assert visible.json()["existing_doc_id"] == first.json()["doc_id"]


def test_gate_requires_bearer_including_loopback(client: TestClient, monkeypatch) -> None:
    _on(monkeypatch)
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "secret")
    assert client.get("/doc/health").status_code == 200
    assert client.get("/doc/library/search").status_code == 401
    allowed = client.get("/doc/library/search", headers={"Authorization": "Bearer secret"})
    assert allowed.status_code == 200


def test_startup_refuses_the_switch_without_a_token(monkeypatch) -> None:
    from mantisfetch_server import _require_owner_scope_token

    _on(monkeypatch)
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MANTISFETCH_MCP_TOKEN"):
        _require_owner_scope_token()


def test_backfill_copies_provenance_and_writes_shared_false(tmp_path, monkeypatch) -> None:
    from mantisfetch_docreader import _backfill_ownership, _get_docs_dir, _update_doc_index

    import mantisfetch_common.storage as cs

    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", docs)
    doc_dir = docs / "General" / "DOC-001"
    doc_dir.mkdir(parents=True)
    manifest = {
        "doc_id": "DOC-001",
        "filename": "old.md",
        "file_type": "md",
        "provenance": {"created_by": "human:alice", "created_via": "agent:councilor"},
    }
    (doc_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _update_doc_index(
        docs,
        {
            "doc_id": "DOC-001",
            "filename": "old.md",
            "file_type": "md",
            "total_pages": 0,
            "section_count": 0,
            "ocr_page_count": 0,
            "table_count": 0,
            "created_at": "2026-01-01T00:00:00Z",
            "storage_path": "General/DOC-001",
        },
        "old",
    )
    # The writer above stamps the new fields. Strip them so this is a legacy row.
    from mantisfetch_common import doc_index_store as dis

    entry = dis.get_document(docs, "DOC-001")
    assert entry is not None
    for key in ("created_by", "created_via", "shared"):
        entry.pop(key, None)
    dis.upsert_document(docs, entry)

    stats = _backfill_ownership(_get_docs_dir())
    assert stats["manifests"] == 1
    assert stats["index"] == 1
    rewritten = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))
    assert rewritten["shared"] is False
    row = dis.get_document(docs, "DOC-001")
    assert row is not None
    assert row["created_by"] == "human:alice"
    assert row["created_via"] == "agent:councilor"
    assert row["shared"] is False


def test_summary_rewrite_keeps_the_latest_shared_flag(tmp_path, monkeypatch) -> None:
    from mantisfetch_docreader import write_output

    import mantisfetch_common.storage as cs

    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", docs)
    parsed = ParsedDocument(
        filename="note.txt",
        file_type="txt",
        total_pages=1,
        pages=[],
        sections=[
            Section(
                index=1,
                title="T",
                level=1,
                text="secretword is here",
                page_range="p.1",
                sid="s_001",
            )
        ],
    )
    write_output(
        "DOC-7",
        parsed,
        "digest",
        "brief",
        docs,
        tags=[],
        source="upload",
        actor=("human:alice", None),
        shared=True,
    )
    # A summary finishing does not know the upload's flag; it must re-read.
    write_output(
        "DOC-7",
        parsed,
        "digest again",
        "brief again",
        docs,
        tags=[],
        source="upload",
        guard_stale_generation=False,
    )
    manifest_path = docs / "DOC-7" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["shared"] is True
    assert manifest["provenance"]["created_by"] == "human:alice"

    manifest["shared"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    write_output(
        "DOC-7",
        parsed,
        "digest after unshare",
        "brief",
        docs,
        tags=[],
        source="upload",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["shared"] is False
    from mantisfetch_common import doc_index_store as dis

    row = dis.get_document(docs, "DOC-7")
    assert row is not None
    assert row["shared"] is False
    assert row["created_by"] == "human:alice"


def test_library_captures_do_not_carry_login_state() -> None:
    """IRP 20261003 D2 / MF 06: a stored capture is anonymous by construction.

    One-shot capture opens a browser context with no storage state. Markdown
    negotiation sends only Accept and User-Agent. Session handlers never
    write a library document, so a logged-in session page cannot become one.
    """
    import mantisfetch_browser as browser
    import mantisfetch_browser.negotiate as negotiate

    capture = inspect.getsource(browser._capture_fresh)
    assert "storage_state" not in capture
    fetch = inspect.getsource(negotiate._fetch)
    assert 'headers = {"Accept": _ACCEPT, "User-Agent": "MantisFetch/negotiate"}' in fetch
    assert "Cookie" not in fetch and "Authorization" not in fetch

    tree = ast.parse(inspect.getsource(browser))
    callers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                func = inner.func
                name = func.id if isinstance(func, ast.Name) else ""
                if name == "_persist_web_capture":
                    callers.add(node.name)
    assert callers
    assert callers.isdisjoint(
        {
            "new_session",
            "goto",
            "distill",
            "act",
            "export_storage_state",
            "webmcp_invoke",
        }
    )

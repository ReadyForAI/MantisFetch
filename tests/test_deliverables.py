"""Tests for the read-only deliverable byte endpoint (IRP 20260711).

Functional cases run through the unified TestClient (a loopback peer, so the REST
gate is open); the fence root is monkeypatched per test. Fence edge cases that are
awkward to express through URL encoding are unit-tested against ``_resolve``
directly. Off-host auth wiring is checked with a non-loopback TestClient.
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

import mantisfetch_deliverables as md


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configured deliverable fence root with one file at P001/T42/report.txt."""
    r = tmp_path / "deliverables"
    (r / "P001" / "T42").mkdir(parents=True)
    (r / "P001" / "T42" / "report.txt").write_text("hello deliverable", encoding="utf-8")
    monkeypatch.setenv("MANTISFETCH_DELIVERABLES_ROOT", str(r))
    return r


def test_unset_root_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """No configured root → the face does not exist (fail-closed)."""
    monkeypatch.delenv("MANTISFETCH_DELIVERABLES_ROOT", raising=False)
    assert client.get("/deliverables/P001/T42/report.txt").status_code == 404


def test_serves_file(client: TestClient, root: Path) -> None:
    resp = client.get("/deliverables/P001/T42/report.txt")
    assert resp.status_code == 200
    assert resp.content == b"hello deliverable"
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-length"] == str(len(b"hello deliverable"))
    assert resp.headers["x-content-type-options"] == "nosniff"
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;") and 'filename="report.txt"' in cd


def test_disposition_inline(client: TestClient, root: Path) -> None:
    resp = client.get("/deliverables/P001/T42/report.txt?disposition=inline")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("inline;")


def test_invalid_disposition_422(client: TestClient, root: Path) -> None:
    resp = client.get("/deliverables/P001/T42/report.txt?disposition=bogus")
    assert resp.status_code == 422


def test_unknown_extension_octet_stream(client: TestClient, root: Path) -> None:
    (root / "blob.unknownext").write_bytes(b"\x00\x01\x02")
    resp = client.get("/deliverables/blob.unknownext")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"


def test_missing_file_404(client: TestClient, root: Path) -> None:
    assert client.get("/deliverables/P001/T42/nope.txt").status_code == 404


def test_directory_404(client: TestClient, root: Path) -> None:
    assert client.get("/deliverables/P001/T42").status_code == 404


def test_dotdot_traversal_404(client: TestClient, root: Path) -> None:
    """A ``..`` escape resolves outside the root → uniform 404, no leak."""
    (root.parent / "secret.txt").write_text("top secret", encoding="utf-8")
    resp = client.get("/deliverables/P001/../../secret.txt")
    assert resp.status_code == 404
    assert b"secret" not in resp.content


def test_symlink_escape_404(client: TestClient, root: Path) -> None:
    """A symlink inside the root pointing out is rejected by realpath containment."""
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("leaked", encoding="utf-8")
    (root / "link.txt").symlink_to(outside / "leak.txt")
    resp = client.get("/deliverables/link.txt")
    assert resp.status_code == 404
    assert b"leaked" not in resp.content


def test_oversized_413(client: TestClient, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file past the cap → 413 (a 0 MB cap makes any non-empty file too large)."""
    monkeypatch.setenv("MANTISFETCH_DELIVERABLES_MAX_MB", "0")
    assert client.get("/deliverables/P001/T42/report.txt").status_code == 413


# ── fence unit tests (awkward to express through URL encoding) ────────────────


def test_resolve_rejects_absolute(tmp_path: Path) -> None:
    assert md._resolve("/etc/passwd", tmp_path) is None


def test_resolve_rejects_dotdot(tmp_path: Path) -> None:
    assert md._resolve("../escape", tmp_path) is None


def test_resolve_hits_regular_file(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    f = tmp_path / "a" / "b.txt"
    f.write_text("x", encoding="utf-8")
    assert md._resolve("a/b.txt", tmp_path.resolve()) == f.resolve()


def test_fence_root_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MANTISFETCH_DELIVERABLES_ROOT", raising=False)
    assert md._fence_root() is None
    monkeypatch.setenv("MANTISFETCH_DELIVERABLES_ROOT", "   ")
    assert md._fence_root() is None


# ── auth wiring: the shared Bearer gate covers /deliverables ──────────────────


def test_offhost_gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mantisfetch_server import app  # noqa: PLC0415

    r = tmp_path / "deliverables"
    r.mkdir()
    (r / "d.txt").write_text("data", encoding="utf-8")
    monkeypatch.setenv("MANTISFETCH_DELIVERABLES_ROOT", str(r))
    off_host = TestClient(app, client=("10.0.0.9", 5555), raise_server_exceptions=False)

    # No token → non-loopback denied at the gate, before routing.
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    assert off_host.get("/deliverables/d.txt").status_code == 403

    # Token set: bearer required, then passes through to serve the file.
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "s3cret")
    assert off_host.get("/deliverables/d.txt").status_code == 401
    ok = off_host.get("/deliverables/d.txt", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and ok.content == b"data"

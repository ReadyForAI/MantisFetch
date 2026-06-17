"""Tests for health endpoints on the unified MantisFetch server."""

from starlette.testclient import TestClient


def test_root_health(client: TestClient) -> None:
    """GET /health returns 200 with ok=True and version info."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "version" in data
    assert "services" in data


def test_web_health(client: TestClient) -> None:
    """GET /web/health returns 200 with ok=True (browser sub-app)."""
    resp = client.get("/web/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


def test_doc_health(client: TestClient) -> None:
    """GET /doc/health returns 200 with ok=True (docreader sub-app)."""
    resp = client.get("/doc/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "supported_formats" in data
    for fmt in ("txt", "json", "jsonl", "xml", "xls", "doc", "ppt"):
        assert fmt in data["supported_formats"]

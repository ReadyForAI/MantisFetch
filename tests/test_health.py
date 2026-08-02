"""Tests for health endpoints on the unified MantisFetch server."""

from starlette.testclient import TestClient

from mantisfetch_common import __version__


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


def test_every_surface_reports_the_same_version(client: TestClient) -> None:
    """Version-reporting surfaces all read the one constant, not their own literal.

    The version used to be copy-pasted at five sites, so a release bump could
    (and did) leave surfaces disagreeing. Assert equality rather than presence —
    the older tests only checked that a "version" key existed, which a stale
    literal passes.
    """
    assert client.get("/health").json()["version"] == __version__
    assert client.get("/doc/health").json()["version"] == __version__
    assert client.get("/openapi.json").json()["info"]["version"] == __version__
    assert client.get("/doc/openapi.json").json()["info"]["version"] == __version__

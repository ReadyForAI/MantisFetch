"""Tests for the REST Bearer gate on the /web and /doc HTTP surface.

The gate is driven directly (pure ASGI) for the matrix, plus one integration
check through the unified TestClient (whose peer is non-loopback "testclient").
"""

import asyncio

from starlette.testclient import TestClient

import mantisfetch_server as ms


def _drive(client_addr, headers=None, path="/session/new"):
    """Run a request through _RestAuthGate; return (status, inner_reached)."""
    reached = {"v": False}

    async def inner(scope, receive, send):
        reached["v"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    gate = ms._RestAuthGate(inner)
    scope = {
        "type": "http", "path": path, "client": client_addr,
        "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(gate(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, reached["v"]


def test_loopback_allowed_without_token(monkeypatch):
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    status, reached = _drive(("127.0.0.1", 5555))
    assert status == 200 and reached


def test_loopback_allowed_even_with_token(monkeypatch):
    # REST gate exempts loopback even when a token is set (differs from the MCP
    # gate, which requires the bearer for every peer once a token is configured).
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "s3cret")
    status, reached = _drive(("127.0.0.1", 5555))
    assert status == 200 and reached


def test_non_loopback_allowed_without_token(monkeypatch):
    # Backward-compat: no token configured → original no-auth behavior.
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    status, reached = _drive(("10.0.0.9", 5555))
    assert status == 200 and reached


def test_non_loopback_blocked_without_bearer_when_token_set(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "s3cret")
    status, reached = _drive(("10.0.0.9", 5555))
    assert status == 401 and not reached
    # spoofing a loopback Host must not help — only the real peer counts
    status2, reached2 = _drive(("10.0.0.9", 5555), headers={"host": "127.0.0.1:9898"})
    assert status2 == 401 and not reached2


def test_non_loopback_allowed_with_correct_bearer(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "s3cret")
    status, reached = _drive(("10.0.0.9", 5555), headers={"authorization": "Bearer s3cret"})
    assert status == 200 and reached


def test_health_exempt_even_off_host_with_token(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "s3cret")
    status, reached = _drive(("10.0.0.9", 5555), path="/health")
    assert status == 200 and reached


def test_integration_gate_through_unified_client(client: TestClient, monkeypatch):
    """End-to-end: the unified TestClient peer is non-loopback, so a token gates
    /doc until the bearer is supplied; /doc/health stays open."""
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "s3cret")
    assert client.get("/doc/library/DOC-X/digest").status_code == 401
    # with the bearer the gate passes through to the handler (404, not 401)
    ok = client.get("/doc/library/DOC-X/digest", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code != 401
    # health is never gated
    assert client.get("/doc/health").status_code == 200

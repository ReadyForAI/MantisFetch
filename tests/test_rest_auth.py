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
    # What a local client sends unless a test says otherwise.
    headers = {"host": "127.0.0.1:9898", **(headers or {})}
    scope = {
        "type": "http", "path": path, "client": client_addr,
        "headers": [(k.encode(), v.encode()) for k, v in headers.items() if v is not None],
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


def test_non_loopback_denied_without_token(monkeypatch):
    # Default-deny (aligned with the MCP gate): non-loopback + no token → 403.
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    status, reached = _drive(("10.0.0.9", 5555))
    assert status == 403 and not reached


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


def test_integration_gate_mounted_on_doc(monkeypatch):
    """End-to-end: the gate is actually mounted on /doc. Uses a non-loopback peer
    (no lifespan needed — the gate is ASGI middleware that runs before routing)."""
    from mantisfetch_server import app  # noqa: PLC0415

    off_host = TestClient(app, client=("10.0.0.9", 5555), raise_server_exceptions=False)

    # No token → non-loopback denied (403), handler never reached.
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    assert off_host.get("/doc/library/DOC-X/digest").status_code == 403
    assert off_host.get("/doc/health").status_code == 200  # health always exempt

    # Token set: bearer required, then passes through to the handler (not 401/403).
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "s3cret")
    assert off_host.get("/doc/library/DOC-X/digest").status_code == 401
    ok = off_host.get("/doc/library/DOC-X/digest", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code not in (401, 403)


# ── Host / Origin on the token-less loopback path (F03) ───────────────────────
# A loopback socket is not proof the caller is a local application: a page in
# a local browser reaching this port through DNS rebinding arrives from
# 127.0.0.1 with the attacker's domain in Host.


def test_a_loopback_peer_naming_a_foreign_host_is_refused(monkeypatch):
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "s3cret")
    status, reached = _drive(("127.0.0.1", 5555), {"host": "untrusted.example:9898"})
    assert status == 403 and not reached


def test_a_loopback_peer_sending_a_foreign_origin_is_refused(monkeypatch):
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    status, reached = _drive(
        ("127.0.0.1", 5555), {"origin": "http://untrusted.example:9898"}, path="/parse"
    )
    assert status == 403 and not reached


def test_an_opaque_null_origin_is_refused(monkeypatch):
    """Sandboxed iframes and file:// pages send `Origin: null`."""
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    status, _ = _drive(("127.0.0.1", 5555), {"origin": "null"})
    assert status == 403


def test_a_loopback_peer_with_no_host_is_refused(monkeypatch):
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    status, _ = _drive(("127.0.0.1", 5555), {"host": None})
    assert status == 403


def test_local_clients_without_an_origin_are_unaffected(monkeypatch):
    """curl, the SDK and same-host services send a loopback Host and no Origin."""
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    for host in ("127.0.0.1:9898", "localhost:9898", "LOCALHOST:9898", "[::1]:9898", "localhost"):
        status, reached = _drive(("127.0.0.1", 5555), {"host": host})
        assert status == 200 and reached, host
    status, _ = _drive(("::1", 5555), {"host": "[::1]:9898"})
    assert status == 200


def test_a_local_page_on_this_service_is_allowed(monkeypatch):
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    for origin in ("http://127.0.0.1:9898", "https://localhost:9898"):
        status, _ = _drive(("127.0.0.1", 5555), {"origin": origin})
        assert status == 200, origin


def test_listed_extra_hosts_are_honoured_with_the_mcp_syntax(monkeypatch):
    """`MANTISFETCH_MCP_ALLOWED_HOSTS` means the same thing on both surfaces."""
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    monkeypatch.setenv("MANTISFETCH_MCP_ALLOWED_HOSTS", "mf.internal:*, proxy.lan:8443")
    assert _drive(("127.0.0.1", 5555), {"host": "mf.internal:9000"})[0] == 200
    assert _drive(("127.0.0.1", 5555), {"host": "proxy.lan:8443"})[0] == 200
    assert _drive(("127.0.0.1", 5555), {"host": "proxy.lan:9999"})[0] == 403
    assert _drive(
        ("127.0.0.1", 5555), {"host": "proxy.lan:8443", "origin": "https://proxy.lan:8443"}
    )[0] == 200


def test_the_bearer_path_does_not_check_host(monkeypatch):
    """A token is a different basis for trust. Checking Host there would break
    every off-host deployment whose name nobody listed."""
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "s3cret")
    status, reached = _drive(
        ("10.0.0.9", 5555),
        {"host": "buildhost.lan:9898", "authorization": "Bearer s3cret"},
    )
    assert status == 200 and reached


def test_health_stays_ungated_whatever_the_host(monkeypatch):
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)
    status, _ = _drive(("127.0.0.1", 5555), {"host": "untrusted.example"}, path="/health")
    assert status == 200


def test_the_unified_app_refuses_a_rebound_library_read(monkeypatch):
    """The report's repro, through the real app: 200 before, 403 now."""
    monkeypatch.setenv("MANTISFETCH_MCP_TOKEN", "s3cret")
    rebound = TestClient(
        ms.app, base_url="http://untrusted.example:9898", client=("127.0.0.1", 5555),
        raise_server_exceptions=False,
    )
    response = rebound.get(
        "/doc/library/search", headers={"Origin": "http://untrusted.example:9898"}
    )
    assert response.status_code == 403

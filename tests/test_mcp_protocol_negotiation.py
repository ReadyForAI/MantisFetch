"""Pin the two MCP protocol eras the /mcp surface serves.

The SDK routes by the `MCP-Protocol-Version` header, not by the `initialize`
body: absent or handshake-era → the legacy path; anything newer → the
single-exchange 2026-07-28 handler. Both are load-bearing (old clients keep
using the handshake), and nothing else in the suite covers either, so a bump
inside `mcp>=2,<3` could drop one silently.
"""

import importlib
import json
from contextlib import asynccontextmanager

import mantisfetch_mcp as mm
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

# The `2026-07-28` request envelope: version + capabilities ride in `params._meta`,
# and the method (plus the tool name, for tools/call) is mirrored into headers.
_MODERN = "2026-07-28"
_LATEST_HANDSHAKE = "2025-11-25"
_META = {
    "io.modelcontextprotocol/protocolVersion": _MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture(scope="module")
def client():
    """Mount the real gated MCP app the way mantisfetch_server does, with the
    session manager running. Loopback peer + a 127.0.0.1 Host so the auth gate and
    the DNS-rebinding check both pass (see _McpAuthGate / _transport_security).

    Reloaded first: a session manager can be run only once per instance, and other
    modules in the suite start the shared one. Reloaded again on teardown so the
    next module inherits an unstarted manager, as test_mcp_search does."""
    importlib.reload(mm)

    @asynccontextmanager
    async def lifespan(app):
        async with mm.mcp.session_manager.run():
            yield

    app = Starlette(lifespan=lifespan, routes=[Mount("/mcp", app=mm.mcp_app)])
    with TestClient(app, base_url="http://127.0.0.1:9898", client=("127.0.0.1", 45678)) as c:
        yield c
    importlib.reload(mm)


def _body(resp):
    """Parse a JSON or SSE-framed JSON-RPC response."""
    text = resp.text
    if "data: " in text:
        text = next(line[6:] for line in text.splitlines() if line.startswith("data: "))
    return json.loads(text)


def test_legacy_initialize_still_negotiates_the_handshake_era(client):
    """A client that sends no MCP-Protocol-Version header keeps the initialize
    handshake and lands on 2025-11-25 — old clients need zero action."""
    resp = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _LATEST_HANDSHAKE,
                "capabilities": {},
                "clientInfo": {"name": "pin-test", "version": "0"},
            },
        },
    )
    assert resp.status_code == 200
    assert _body(resp)["result"]["protocolVersion"] == _LATEST_HANDSHAKE


def test_legacy_path_issues_no_session_id(client):
    """stateless_http=True: no Mcp-Session-Id is minted, so nothing for a client to
    carry (and a stale one it does carry is ignored rather than 404'd)."""
    resp = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream", "Mcp-Session-Id": "stale-id"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _LATEST_HANDSHAKE,
                "capabilities": {},
                "clientInfo": {"name": "pin-test", "version": "0"},
            },
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("mcp-session-id") is None


def test_modern_header_routes_to_the_envelope_face(client):
    """The `MCP-Protocol-Version` header is what picks the era. Send it without the
    2026-07-28 request envelope and the modern handler rejects the call — an
    enforcement that exists ONLY on that face, which is what makes this a routing
    test rather than a "tools/list answered" test (the legacy stateless path answers
    tools/list too)."""
    resp = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _MODERN,
            "mcp-method": "tools/list",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    error = _body(resp)["error"]
    assert error["code"] == -32602
    assert "io.modelcontextprotocol/protocolVersion" in error["message"]


def test_modern_era_serves_tools_with_the_envelope(client):
    """The 2026-07-28 face proper: a self-contained POST (no initialize handshake)
    carrying the envelope lists the tools. This is the face agentd negotiates."""
    resp = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _MODERN,
            "mcp-method": "tools/list",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _META}},
    )
    assert resp.status_code == 200
    names = {t["name"] for t in _body(resp)["result"]["tools"]}
    assert "doc_search" in names and "web_capture" in names


def test_modern_era_dispatches_a_tool_call(client):
    """tools/call over the same single-exchange face reaches the tool body (the
    library is empty in tests, so a clean result envelope is the success signal)."""
    resp = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _MODERN,
            "mcp-method": "tools/call",
            "mcp-name": "doc_search",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "doc_search", "arguments": {"q": "pin-test"}, "_meta": _META},
        },
    )
    result = _body(resp)["result"]
    # `result` alone is not success — a tool that raised also returns one, with
    # isError set. The dispatch only counts if the tool body ran clean.
    assert result.get("isError") is not True


def test_installed_sdk_still_offers_the_2026_07_28_protocol():
    """`mcp>=2,<3` must keep providing 2026-07-28 — the version agentd negotiates.
    Membership, not latest-ness: a later revision may be added on top, and that is
    fine; dropping this one is what would strand the modern face."""
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS

    assert _MODERN in MODERN_PROTOCOL_VERSIONS


def test_modern_era_declares_its_tool_list_immediately_stale(client):
    """SEP-2549 freshness hints on the 2026-07-28 face. `CacheableResult` defaults
    to `ttl_ms=0` / `cache_scope="private"` and MantisFetch passes no
    `cache_hints`, so the wire says "immediately stale, do not share across
    authorization contexts" — a default speaking for the server. Pinned because
    that declaration is what conformant clients act on, and a bump inside
    `mcp>=2,<3` could change it without touching a line here. Whether the value
    should stay 0 is docked to the 20260723 distribution IRP; this only pins what
    is currently declared."""
    resp = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _MODERN,
            "mcp-method": "tools/list",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _META}},
    )
    result = _body(resp)["result"]
    assert result["ttlMs"] == 0
    assert result["cacheScope"] == "private"


def test_legacy_era_omits_the_cache_hints_entirely(client):
    """The other half of the same fact: emission is version-gated, not
    unconditional. The per-version surface sieves `ttlMs`/`cacheScope` off a
    handshake-era result, so one endpoint answers the same `tools/list` with the
    hints on one face and without them on the other. A client keying off these
    must tell absent (upstream too old to say) apart from 0 (upstream saying
    "stale now")."""
    client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _LATEST_HANDSHAKE,
                "capabilities": {},
                "clientInfo": {"name": "pin-test", "version": "0"},
            },
        },
    )
    resp = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _LATEST_HANDSHAKE,
        },
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    result = _body(resp)["result"]
    # A listing that actually happened — otherwise "key absent" would pass on an
    # error result too.
    assert result["tools"]
    assert "ttlMs" not in result
    assert "cacheScope" not in result

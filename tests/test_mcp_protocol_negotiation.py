"""Pin the two MCP protocol eras the /mcp surface serves.

SharedSpecs notice `mcp-20260728-adaptation` (SS #502): agentd now negotiates
2026-07-28 on both faces, and the repos that talk to it re-verify their own SDK
once. MantisFetch is on `mcp>=2,<3` (2.0.0), whose streamable-HTTP transport
routes by the `MCP-Protocol-Version` header: a request without one (or with a
handshake-era value) takes the legacy `initialize` path, anything newer takes the
single-exchange 2026-07-28 path. Both eras are load-bearing here — NodalOS keeps
serving old clients, and a bump inside the `<3` range must not silently drop
either one — so pin them.
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
    assert resp.status_code == 200
    assert "result" in _body(resp)


def test_installed_sdk_offers_the_2026_07_28_protocol():
    """The SDK generation itself — `mcp>=2,<3` must keep providing 2026-07-28, which
    is the version agentd negotiates. A bump inside the range that dropped it would
    silently strand the modern face."""
    from mcp.types import LATEST_PROTOCOL_VERSION

    assert LATEST_PROTOCOL_VERSION == _MODERN

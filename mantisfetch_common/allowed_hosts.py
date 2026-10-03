"""Which Host and Origin values a same-host caller may present.

Two surfaces need the same answer. The MCP transport has the SDK's DNS-rebinding
check; the REST surface (/web, /doc, /deliverables) trusted any loopback peer
outright, and a loopback peer is exactly what a malicious page reaching this
host through DNS rebinding looks like — the socket is local, the Host is the
attacker's domain. Building the list in one place keeps the two from drifting.

Matching follows the MCP SDK's ``TransportSecuritySettings`` so the
``MANTISFETCH_MCP_ALLOWED_HOSTS`` syntax means the same on both: exact, or
``host:*`` for any port.
"""

from __future__ import annotations

import os

__all__ = ["allowed_hosts_and_origins", "host_allowed", "origin_allowed"]


def allowed_hosts_and_origins() -> tuple[list[str], list[str]]:
    """Loopback host[:port] values, their http/https origins, plus the extras
    from ``MANTISFETCH_MCP_ALLOWED_HOSTS`` (comma-separated).

    Origins cover both schemes: a browser/Electron client sends
    ``Origin: https://<host>`` once the server runs with TLS.
    """
    port = os.environ.get("PORT", "9898")
    loopback = ("127.0.0.1", "localhost", "[::1]")
    hosts = [f"{h}:{port}" for h in loopback] + list(loopback)
    origins: list[str] = []
    for h in loopback:
        origins += [f"http://{h}:{port}", f"https://{h}:{port}"]
        # A browser leaves the default port out of Origin. Only for the scheme
        # whose default it is: a bare http://localhost on any other port is a
        # different origin, and possibly someone else's page.
        if port == "80":
            origins.append(f"http://{h}")
        elif port == "443":
            origins.append(f"https://{h}")
    for extra in os.environ.get("MANTISFETCH_MCP_ALLOWED_HOSTS", "").split(","):
        extra = extra.strip()
        if extra:
            hosts.append(extra)
            origins += [f"http://{extra}", f"https://{extra}"]
    return hosts, origins


def _matches(value: str, allowed: list[str]) -> bool:
    # Host names are case-insensitive on both sides: a listed MF.internal has
    # to match the mf.internal a client sends, and the reverse.
    value = value.lower()
    allowed = [a.lower() for a in allowed]
    if value in allowed:
        return True
    return any(a.endswith(":*") and value.startswith(a[:-2] + ":") for a in allowed)


def host_allowed(host: str | None, allowed: list[str]) -> bool:
    """A missing Host is refused, as the SDK refuses it."""
    return bool(host) and _matches(host, allowed)


def origin_allowed(origin: str | None, allowed: list[str]) -> bool:
    """No Origin is fine — it is what curl, SDKs and same-origin requests send."""
    return not origin or _matches(origin, allowed)

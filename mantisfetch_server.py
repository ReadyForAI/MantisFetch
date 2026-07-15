"""MantisFetch unified server entry point.

Mounts the browser service at /web and the document reader at /doc on a
single FastAPI instance, served on port 9898 by default.

Final API surface (single port 9898):
  GET  /health                               — aggregated health check
  GET  /web/health                           — browser service status
  POST /web/session/{new,goto,distill,...}   — browser session operations
  GET  /doc/health                           — docreader service status
  POST /doc/parse                            — upload and parse document
  GET  /doc/library/search                   — search document library
  GET  /doc/library/{doc_id}/{digest,brief,full,sections,section/{sid},table/{tid},manifest}
  GET  /deliverables/{rel_path}              — read-only deliverable byte face
"""

import logging
import os
import secrets
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from mantisfetch_deliverables import deliverables_app

logger = logging.getLogger("mantisfetch")

# Add service directories to sys.path so modules can be imported by name.
# Must precede the service imports below.
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT / "services" / "browser"))
sys.path.insert(0, str(_ROOT / "services" / "docreader"))
sys.path.insert(0, str(_ROOT / "services" / "mcp"))

from mantisfetch_browser import app as browser_app  # noqa: E402
from mantisfetch_docreader import app as doc_app  # noqa: E402
from mantisfetch_mcp import mcp, mcp_app  # noqa: E402


def _warn_legacy_env() -> None:
    """Warn about pre-rename ``LARKSCOUT_*`` environment variables.

    MantisFetch reads only ``MANTISFETCH_*``; a leftover ``LARKSCOUT_*`` config
    from the LarkScout era would otherwise fail silently (the service would run
    on defaults). Surfacing it at startup turns a silent misconfiguration into a
    visible one.
    """
    legacy = sorted(k for k in os.environ if k.startswith("LARKSCOUT_"))
    if legacy:
        logger.warning(
            "Ignoring %d legacy LARKSCOUT_* environment variable(s): %s. "
            "MantisFetch reads MANTISFETCH_* only — rename them "
            "(e.g. LARKSCOUT_LLM_API_KEY -> MANTISFETCH_LLM_API_KEY).",
            len(legacy),
            ", ".join(legacy),
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Start sub-application lifespans (browser Playwright init, docreader startup
    tasks) plus the MCP server's streamable-HTTP session manager."""
    _warn_legacy_env()
    async with browser_app.router.lifespan_context(browser_app):
        async with doc_app.router.lifespan_context(doc_app):
            async with mcp.session_manager.run():
                yield


app = FastAPI(
    title="MantisFetch",
    version="1.5.0",
    description="Open-source data collection and document parsing platform by ReadyForAI.",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    """Return aggregated health status for all mounted services."""
    return {
        "ok": True,
        "version": "1.5.0",
        "services": {
            "browser": "mounted at /web",
            "docreader": "mounted at /doc",
        },
    }


class _RestAuthGate:
    """Pure-ASGI Bearer gate for the /web, /doc and /deliverables HTTP surface
    (SSE-safe — only ever emits its own response on deny, otherwise passes through
    untouched).

    The same browser-driving / doc-parsing capabilities the MCP gate locks down
    are reachable directly on /web/* and /doc/*; once the server binds 0.0.0.0
    (needed for cross-host MCP) those would otherwise be wide open. Behavior
    (loopback-only by default, matching the MCP gate):

    - loopback peer (127.0.0.1 / ::1): always allowed — same-host callers,
      including Skeleton-Doc over the Docker bridge when it shares the host, are
      unaffected.
    - ``MANTISFETCH_MCP_TOKEN`` set: require that bearer for non-loopback peers
      (constant-time compare; else 401). A cross-host / cross-bridge Agent reaches
      the surface by presenting the token.
    - non-loopback + token unset: denied (403). Closes the default footgun where
      ``docker compose up`` (publishing 9898) would otherwise expose file upload,
      browser control and login-state export to the LAN. Set the token to allow
      authenticated off-host access.
    - health endpoints are never gated, for liveness probes.

    The real socket peer (``scope["client"]``) is used, never the spoofable Host
    / X-Forwarded-For header.
    """

    _LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
    # The mount does not rewrite scope["path"], so the gate sees the full path.
    # Both the stripped and full forms are exempted to be robust across Starlette
    # versions.
    _HEALTH_PATHS = {"/health", "/web/health", "/doc/health"}

    def __init__(self, app: object) -> None:
        self.app = app

    def _deny(self, scope: dict) -> tuple[int, bytes] | None:
        client = scope.get("client")
        peer = client[0] if client else None
        if peer in self._LOOPBACK:
            return None
        token = os.environ.get("MANTISFETCH_MCP_TOKEN")
        if not token:
            return 403, (
                b'{"error":"forbidden: this surface is loopback-only; '
                b'set MANTISFETCH_MCP_TOKEN to allow non-loopback clients"}'
            )
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode()
        if not secrets.compare_digest(provided, f"Bearer {token}"):
            return 401, b'{"error":"unauthorized"}'
        return None

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] == "http" and scope.get("path") not in self._HEALTH_PATHS:
            denied = self._deny(scope)
            if denied is not None:
                status, body = denied
                await send({
                    "type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# Browser / docreader routes are clean (no /web /doc prefix internally) — mount
# directly, behind the REST Bearer gate (loopback-open; token-gated off-host).
app.mount("/web", _RestAuthGate(browser_app))
app.mount("/doc", _RestAuthGate(doc_app))

# Read-only deliverable byte face (IRP 20260711): serves agent deliverables from
# under MANTISFETCH_DELIVERABLES_ROOT for AULO's BFF to proxy. Same Bearer gate as
# /web /doc; the fence is unrelated to the library (deliverables carry no doc_id).
app.mount("/deliverables", _RestAuthGate(deliverables_app))

# MCP server (streamable-HTTP) — a thin front-end exposing /web + /doc as Model
# Context Protocol tools. Its session manager is started in the lifespan above.
app.mount("/mcp", mcp_app)


def _ssl_kwargs() -> dict[str, str]:
    """uvicorn TLS kwargs from the environment, or ``{}`` for plain http.

    Set both MANTISFETCH_TLS_CERTFILE and MANTISFETCH_TLS_KEYFILE to serve https
    (e.g. for a non-loopback MCP client like NodalOS, where the bearer token must
    ride an encrypted line). Both are required — setting only one is treated as
    unset (plain http) rather than a half-configured TLS that would fail to boot.
    """
    certfile = os.environ.get("MANTISFETCH_TLS_CERTFILE")
    keyfile = os.environ.get("MANTISFETCH_TLS_KEYFILE")
    if certfile and keyfile:
        return {"ssl_certfile": certfile, "ssl_keyfile": keyfile}
    return {}


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "9898"))
    uvicorn.run(app, host=host, port=port, **_ssl_kwargs())

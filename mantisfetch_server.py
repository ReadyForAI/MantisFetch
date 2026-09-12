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

from mantisfetch_common import __version__
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


def _warn_unconfigured_llm() -> None:
    """Say at startup which LLM roles cannot run, instead of on the first document.

    No model name is invented anywhere any more, so a deployment that never set
    one now fails when it first tries to summarize or OCR — better than talking
    to a model the code guessed, but still late and far from the cause.

    Deliberately a warning rather than a refusal to start: parsing, capture and
    local OCR are complete features that need no LLM at all, and a deployment
    using only those must keep working. Building the provider and asking it
    ``check_configuration()`` costs no network, so this is just the same error,
    surfaced at boot.
    """
    from providers import get_provider  # noqa: PLC0415

    for role in ("summary", "ocr"):
        try:
            get_provider(role).check_configuration()
        except Exception as exc:  # noqa: BLE001 - report, never block startup
            logger.warning(
                "LLM role %r is not usable: %s — %s features will fail until this "
                "is configured. Parsing, capture and local OCR are unaffected.",
                role,
                exc,
                "summarisation" if role == "summary" else "LLM OCR",
            )


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
    _warn_unconfigured_llm()
    async with browser_app.router.lifespan_context(browser_app):
        async with doc_app.router.lifespan_context(doc_app):
            async with mcp.session_manager.run():
                yield


app = FastAPI(
    title="MantisFetch",
    version=__version__,
    description="Open-source data collection and document parsing platform by ReadyForAI.",
    lifespan=lifespan,
)


def _llm_role_status() -> dict[str, str]:
    """Per-role model name, or the reason the role cannot run."""
    from providers import get_provider  # noqa: PLC0415

    status: dict[str, str] = {}
    for role in ("summary", "ocr"):
        try:
            provider = get_provider(role)
            provider.check_configuration()
        except Exception as exc:  # noqa: BLE001 - health must not raise
            status[role] = f"unconfigured: {exc}"
            continue
        status[role] = provider.describe_model(role)
    return status


@app.get("/health")
async def health() -> dict:
    """Return aggregated health status for all mounted services."""
    return {
        "ok": True,
        "version": __version__,
        "services": {
            "browser": "mounted at /web",
            "docreader": "mounted at /doc",
        },
        # Which model each LLM role resolved to, or why it did not. There is no
        # built-in model name to fall back on, so "unconfigured" here is the
        # difference between "summaries are off" and "summaries are broken" —
        # and it names the key to set.
        "llm": _llm_role_status(),
    }


@app.get("/metrics")
async def metrics() -> dict:
    """Process-wide cumulative counters (token efficiency, cache, failover).

    Ungated like ``/health`` so operators can scrape without a bearer token.
    Counters reset on process restart.
    """
    from mantisfetch_common.metrics import snapshot

    return {"ok": True, "metrics": snapshot()}


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
    _HEALTH_PATHS = {"/health", "/metrics", "/web/health", "/doc/health"}

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
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def _max_request_bytes() -> int:
    """The largest request body the upload surface will read.

    Derived from the per-file limit rather than configured separately, so there
    is one number to raise. The slack covers the multipart envelope — boundaries
    and the other form fields — around a file that is itself at the limit.

    Read per call, like the other tunables, so a test or a redeploy that changes
    MANTISFETCH_MAX_UPLOAD_MB is seen.
    """
    from mantisfetch_docreader import _max_request_bytes as _doc_max  # noqa: PLC0415

    return _doc_max()


def _body_too_large_bases() -> tuple[type[BaseException], ...]:
    """What the ceiling's sentinel must be, for the parser to clean up after it.

    Starlette's multipart parser closes the files it has already spooled only
    for the exception types its own `except` names, and which those are depends
    on the version: current releases catch `MultiPartException` and `OSError`,
    older ones in our supported range catch `MultiPartException` alone. Being
    both means the cleanup runs either way rather than only on the version that
    happens to be installed here.
    """
    try:
        from starlette.formparsers import MultiPartException  # noqa: PLC0415

        return (MultiPartException, OSError)
    except Exception:  # pragma: no cover - Starlette moved it
        return (OSError,)


class _BodyTooLarge(*_body_too_large_bases()):  # type: ignore[misc]
    """Raised into the body stream once a request passes the ceiling."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"request body exceeds the {limit}-byte ceiling")


class _BodyCeiling:
    """Stop reading a request body once it passes the ceiling, and answer 413.

    The per-file limits run inside the handlers, which is *after* Starlette has
    parsed the multipart body and spooled every byte of it: they bound what the
    service keeps, never what a caller can make it receive. Measured, a 4 KiB
    body against a 16-byte limit landed in full before the 413.

    This sits in front of the form parser instead. A declared Content-Length
    over the ceiling is refused without reading anything; a chunked body is
    counted as it arrives and abandoned at the first chunk that crosses. It does
    not try to drain what the client is still sending — the connection ends.

    Deliberately not in front of ``/mcp``: that surface has its own body limit,
    derived from the inline-document cap, and the SDK's transport reads the body
    itself. A second counter there would be a second thing to keep in step.

    The per-request limits stay where they are. MAX_UPLOAD_BYTES and the raw
    channel's per-type ceilings both need the filename to decide, which is only
    known once the form is parsed — so this is the outer bound, not a
    replacement for either.
    """

    def __init__(self, app: object) -> None:
        self.app = app

    @staticmethod
    async def _refuse(send: object, limit: int) -> None:
        body = (f'{{"detail":"request body exceeds the {limit}-byte ceiling"}}').encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = _max_request_bytes()
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._refuse(send, limit)
                    return
            except ValueError:
                pass

        received = 0
        refused = False

        async def counting_receive() -> dict:
            nonlocal received, refused
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    refused = True
                    # Raised rather than returned, and an OSError specifically.
                    # Starlette's multipart parser closes the spool files it has
                    # opened when the stream raises MultiPartException or
                    # OSError — and nothing else. A disconnect (ClientDisconnect)
                    # or a truncated body leaves them open until the garbage
                    # collector runs, which is exactly the disk this ceiling
                    # exists to bound. The sentinel is caught below.
                    raise _BodyTooLarge(limit)
            return message

        async def guarded_send(message: dict) -> None:
            if refused and message["type"] == "http.response.start":
                await self._refuse(send, limit)
                return
            if refused and message["type"] == "http.response.body":
                return
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except _BodyTooLarge:
            # The parser has closed its spool files on the way out; nothing has
            # been sent yet, so the 413 is this response.
            await self._refuse(send, limit)
        if refused and received:
            logger.warning(
                "refused a request body over the %d-byte ceiling after %d bytes",
                limit,
                received,
            )


# Browser / docreader routes are clean (no /web /doc prefix internally) — mount
# directly, behind the REST Bearer gate (loopback-open; token-gated off-host).
# The body ceiling wraps the gate: an oversized body should not be read even to
# find out whether the caller is authorised.
app.mount("/web", _BodyCeiling(_RestAuthGate(browser_app)))
app.mount("/doc", _BodyCeiling(_RestAuthGate(doc_app)))

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

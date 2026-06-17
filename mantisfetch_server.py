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
"""

import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

# Add service directories to sys.path so modules can be imported by name.
# Must precede the service imports below.
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT / "services" / "browser"))
sys.path.insert(0, str(_ROOT / "services" / "docreader"))
sys.path.insert(0, str(_ROOT / "services" / "mcp"))

from mantisfetch_browser import app as browser_app  # noqa: E402
from mantisfetch_docreader import app as doc_app  # noqa: E402
from mantisfetch_mcp import mcp, mcp_app  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Start sub-application lifespans (browser Playwright init, docreader startup
    tasks) plus the MCP server's streamable-HTTP session manager."""
    async with browser_app.router.lifespan_context(browser_app):
        async with doc_app.router.lifespan_context(doc_app):
            async with mcp.session_manager.run():
                yield


app = FastAPI(
    title="MantisFetch",
    version="0.1.0",
    description="Open-source data collection and document parsing platform by ReadyForAI.",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    """Return aggregated health status for all mounted services."""
    return {
        "ok": True,
        "version": "0.1.0",
        "services": {
            "browser": "mounted at /web",
            "docreader": "mounted at /doc",
        },
    }


# Browser routes are clean (no /web prefix internally) — mount directly.
app.mount("/web", browser_app)

# Docreader routes are clean (no /doc prefix internally after the fix to
# /doc/parse → /parse) — mount directly.
app.mount("/doc", doc_app)

# MCP server (streamable-HTTP) — a thin front-end exposing /web + /doc as Model
# Context Protocol tools. Its session manager is started in the lifespan above.
app.mount("/mcp", mcp_app)


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "9898"))
    uvicorn.run(app, host=host, port=port)

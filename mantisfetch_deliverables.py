"""Read-only deliverable byte endpoint (IRP 20260711-deliverable-preview-download).

Serves agent deliverable files from under a configured fence root so AULO's BFF
can proxy previews/downloads. This is the PC-standalone counterpart to the
full-stack Harness ``GET /api/deliverables/*`` endpoint; both are byte faces only.
The deliverable never enters the MantisFetch library/index (no doc_id, orthogonal
to parse/GC). Read-only: no write or delete surface.

Mounted at ``/deliverables`` on the unified :9898 app behind the shared REST
Bearer gate (loopback-open; token-gated off-host), the same credential as upload.
"""

import logging
import mimetypes
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

from mantisfetch_common.http_headers import content_disposition
from mantisfetch_common.paths import open_within as _open_within

logger = logging.getLogger("mantisfetch_deliverables")

# No docs/openapi routes: this is a byte face, not a browsable API. Their default
# paths would also 200 while the fence is disabled (contradicting the unset → 404
# contract) and shadow deliverables literally named ``docs`` / ``openapi.json``.
deliverables_app = FastAPI(
    title="MantisFetch deliverables",
    description="Read-only deliverable byte endpoint.",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_CHUNK = 64 * 1024

# Content types safe to render inline (passive — the browser won't execute them).
# Anything else requested inline is coerced to attachment so untrusted, agent-
# generated deliverable HTML/SVG can never run as active same-origin content on
# the loopback-open :9898 surface (where it could reach /web and /doc).
_SAFE_INLINE_TYPES = frozenset(
    {
        "application/pdf",
        "text/plain",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
    }
)


def _fence_root() -> Path | None:
    """The deliverable fence root, or ``None`` when unset/empty.

    No default — an unconfigured root means the download face does not exist
    (every request 404s, fail-closed). Deliberately distinct from
    ``MANTISFETCH_DOCS_DIR`` (library storage) and ``MANTISFETCH_ALLOWED_DOC_ROOTS``
    (the ``doc_parse`` fence); deployment must not point this root inside either.
    """
    raw = os.environ.get("MANTISFETCH_DELIVERABLES_ROOT", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve(strict=False)


def _max_bytes() -> int:
    raw = os.environ.get("MANTISFETCH_DELIVERABLES_MAX_MB", "200").strip()
    try:
        mb = int(raw)
    except ValueError:
        logger.warning(
            "MANTISFETCH_DELIVERABLES_MAX_MB=%r is not an integer; using default 200",
            raw,
        )
        mb = 200
    # 0 is valid (= any non-empty file is over limit). Negative is nonsense.
    if mb < 0:
        logger.warning(
            "MANTISFETCH_DELIVERABLES_MAX_MB=%r is negative; using default 200",
            raw,
        )
        mb = 200
    return mb * 1024 * 1024


def _resolve(rel_path: str, root: Path) -> Path | None:
    """Resolve ``rel_path`` under ``root`` with containment, or ``None``.

    Returns ``None`` (→ a uniform 404) for an absolute path, any ``..`` component,
    an escape past the root after symlink resolution, or a target that is not a
    regular file. The uniform miss is intentional: unlike the ingest protocol
    (#166, which distinguishes TTL-expired from fenced), a public byte endpoint
    must not leak which paths exist.
    """
    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    try:
        candidate = (root / rel).resolve(strict=False)
    except (OSError, ValueError):
        return None  # invalid path (e.g. an embedded NUL byte) → a uniform miss
    if not candidate.is_relative_to(root):
        return None
    if not candidate.is_file():
        return None
    return candidate


def _iter_fd(fd: int) -> Iterator[bytes]:
    with os.fdopen(fd, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            yield chunk


@deliverables_app.get("/{rel_path:path}")
async def get_deliverable(
    rel_path: str,
    disposition: str = Query("attachment"),
) -> StreamingResponse:
    """Stream one deliverable file's bytes from under the fence root.

    ``disposition`` is ``attachment`` (default) or ``inline``; ``inline`` is
    honored only for passive types (``_SAFE_INLINE_TYPES``) and otherwise coerced
    to ``attachment``. Streams in chunks — no whole-file read; Range is not
    served in v1.
    """
    root = _fence_root()
    if root is None:
        raise HTTPException(404, "not found")
    if disposition not in ("inline", "attachment"):
        raise HTTPException(422, "disposition must be 'inline' or 'attachment'")
    target = _resolve(rel_path, root)
    if target is None:
        raise HTTPException(404, "not found")
    # Open via a no-follow descriptor walk (race-free against symlink swaps at any
    # component), validate through the descriptor, then stream from it. The size cap
    # is authoritative for the exact bytes served; fd ownership passes to _iter_fd
    # only on success.
    try:
        fd = _open_within(root, target)
    except (OSError, ValueError):
        raise HTTPException(404, "not found") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HTTPException(404, "not found")
        max_bytes = _max_bytes()
        if st.st_size > max_bytes:
            raise HTTPException(
                413,
                f"deliverable too large: {st.st_size} bytes (max {max_bytes}; raise "
                "MANTISFETCH_DELIVERABLES_MAX_MB or fetch the file out of band)",
            )
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if disposition == "inline" and media_type not in _SAFE_INLINE_TYPES:
            disposition = "attachment"  # never serve active content as same-origin inline
        headers = {
            "Content-Length": str(st.st_size),
            "Content-Disposition": content_disposition(disposition, target.name),
            "X-Content-Type-Options": "nosniff",
        }
        return StreamingResponse(_iter_fd(fd), media_type=media_type, headers=headers)
    except Exception:
        os.close(fd)
        raise

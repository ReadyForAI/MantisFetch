"""URL validation (anti-SSRF) for the browser service.

`_validate_url` is the gate every navigation/capture request passes through: it
rejects non-HTTP(S) schemes and targets that resolve to private / loopback /
reserved addresses (and obvious localhost aliases). Self-contained leaf — only
the stdlib and FastAPI's HTTPException.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from fastapi import HTTPException

_ALLOWED_SCHEMES = {"http", "https"}


def _validate_url(url: str) -> None:
    """Block non-HTTP schemes and requests to private/loopback networks."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise HTTPException(400, f"URL scheme not allowed: {parsed.scheme!r}")
    hostname = parsed.hostname or ""
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise HTTPException(400, f"URL target is a private/reserved address: {hostname}")
    except ValueError:
        # hostname is a domain name — resolve is left to Playwright;
        # block obvious localhost aliases
        if hostname.lower() in ("localhost", "localhost.localdomain"):
            raise HTTPException(400, f"URL target not allowed: {hostname}")

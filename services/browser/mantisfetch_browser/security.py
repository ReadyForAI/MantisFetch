"""URL validation (anti-SSRF) for the browser service.

Two layers guard every navigation:

- `_validate_url(url)` — synchronous, no DNS. Rejects non-HTTP(S) schemes,
  localhost / cloud-metadata host aliases, and IP literals in any notation
  (dotted, decimal, hex, octal) that point at a private / loopback / link-local
  / reserved / non-global address. Used as a fast pre-check in request handlers
  so it never blocks the event loop on DNS.
- `_url_allowed(url)` — same checks plus DNS resolution (so a domain whose
  A/AAAA record points at a private address is rejected). Used by the
  context-level network route guard, which runs it off-thread and aborts
  redirects / popups that resolve to disallowed targets.

Residual risk: DNS rebinding (resolution differs between check and fetch) is
not fully closed here; a complete fix pins the validated IP at the connection
layer or runs the browser in a network namespace with no route to RFC1918 /
link-local. Self-contained leaf — only stdlib and FastAPI's HTTPException.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

_ALLOWED_SCHEMES = {"http", "https"}

# Host aliases that name a local / metadata target without being an IP literal.
_BLOCKED_HOST_NAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
}


def _ip_disallowed(ip: ipaddress._BaseAddress) -> bool:
    """True for any address we must not let the browser reach."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def _literal_ips(hostname: str) -> list[ipaddress._BaseAddress]:
    """IPs the hostname denotes as a literal (incl. decimal/hex/octal IPv4)."""
    ips: list[ipaddress._BaseAddress] = []
    try:
        # inet_aton accepts dotted, decimal, hex (0x) and octal (0) forms, which
        # ipaddress.ip_address rejects — these are the classic SSRF bypasses.
        ips.append(ipaddress.ip_address(socket.inet_aton(hostname)))
    except OSError:
        pass
    try:
        ips.append(ipaddress.ip_address(hostname))  # normal IPv4 + IPv6 literals
    except ValueError:
        pass
    return ips


def _resolved_ips(hostname: str) -> list[ipaddress._BaseAddress]:
    """IPs the hostname resolves to via DNS (best effort)."""
    ips: list[ipaddress._BaseAddress] = []
    try:
        for info in socket.getaddrinfo(hostname, None):
            try:
                ips.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    except OSError:
        pass
    return ips


def _url_violation(url: str, *, resolve: bool) -> str | None:
    """Return a rejection reason for the URL, or None if it is allowed.

    With ``resolve=False`` only scheme, host aliases and IP literals are
    checked (no DNS). With ``resolve=True`` the hostname is also resolved and
    every resulting address is checked.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"URL scheme not allowed: {parsed.scheme!r}"
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        return "URL has no host"
    if hostname in _BLOCKED_HOST_NAMES or hostname.endswith(".localhost"):
        return f"URL target not allowed: {hostname}"

    ips = _literal_ips(hostname)
    if not ips and resolve:
        ips = _resolved_ips(hostname)
        if not ips:
            # Fail closed: this guard's resolver returned nothing, but Chromium
            # runs its own resolution at fetch time and may reach a private
            # address (split-horizon / transient SERVFAIL). Block rather than
            # defer to the browser. (The DNS-free pre-check, resolve=False,
            # stays permissive for domains — the route guard does the real check.)
            return f"URL host could not be resolved: {hostname}"
    for ip in ips:
        if _ip_disallowed(ip):
            return f"URL target is a private/reserved address: {hostname}"
    return None


def _validate_url(url: str) -> None:
    """Fast, DNS-free anti-SSRF pre-check; raises HTTP 400 if disallowed."""
    msg = _url_violation(url, resolve=False)
    if msg:
        raise HTTPException(400, msg)


def _url_allowed(url: str) -> bool:
    """Full anti-SSRF check incl. DNS resolution; returns True if allowed."""
    return _url_violation(url, resolve=True) is None


# RFC 2544 benchmarking range. Fake-ip proxies hand it out for every domain they
# resolve, so seeing it here means the host is behind one rather than that the
# caller asked for a benchmark address.
_FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")


def blocked_target_hint(url: str) -> str | None:
    """Why a navigation to ``url`` was refused by the guard, if it was.

    The route guard aborts with "addressunreachable", which Chromium surfaces as
    ``net::ERR_ADDRESS_UNREACHABLE`` — indistinguishable from the network being
    down. On a host behind a fake-ip proxy (Clash / Mihomo / sing-box in TUN
    mode) every domain resolves into 198.18.0.0/15, which is reserved and
    correctly refused, so /web appears to fail with a network error on every
    single page. That cost a full debugging session to identify.

    Returns None when the address looks fine and the failure was something else.
    Resolves once, on the failure path only.
    """
    try:
        hostname = urlparse(url).hostname
    except ValueError:
        return None
    if not hostname:
        return None
    ips: list[ipaddress._BaseAddress] = []
    try:
        for info in socket.getaddrinfo(hostname, None):
            try:
                ips.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    except OSError:
        return None
    bad = [ip for ip in ips if _ip_disallowed(ip)]
    if not bad or len(bad) != len(ips):
        return None
    msg = f"blocked by SSRF policy: {hostname} resolves to {bad[0]}, not a public address"
    # Only volunteer the proxy explanation when the address looks like a fake-ip
    # mapping. For a host that genuinely names a private target, saying "a proxy
    # did this" would send the reader down the wrong path.
    if any(ip.version == 4 and ip in _FAKE_IP_RANGE for ip in bad):
        msg += (
            ". That range is what a fake-ip proxy (Clash/Mihomo/sing-box in TUN "
            "mode) maps every domain into, which makes /web unusable host-wide"
        )
    return msg

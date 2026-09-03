"""Ask the site for markdown before opening a browser.

/web/capture launches a Playwright context for every URL, a static documentation
page included. That is wall clock, memory, and one of only ten slots in
``_capture_sem``. When a site will simply hand over markdown, none of it is
needed — and the markdown is already clean, so the whole extraction problem is
skipped rather than solved.

The ladder, cheapest first (agent-browser's ``read.rs`` does the same):

  1. ``Accept: text/markdown`` on the requested URL
  2. the ``.md`` path variant — ``/docs/x`` -> ``/docs/x.md``, ``/`` -> ``/index.md``
  3. ``llms.txt`` from the nearest ancestors, following the per-page link the
     index gives for this URL

Each rung has three outcomes, not two:

  hit     markdown or plain text with a 2xx  -> use it
  miss    HTML, or 404                       -> try the next rung
  refuse  5xx                                -> give up entirely

The miss case has to include *HTML with a 200*. ``Accept`` is a request header a
server may ignore, and an SPA's "not found" page is almost always 200 + HTML.
Treating that as a hit would put an error page in the library, which is the bug
/web/capture was just fixed not to have.

Off by default. See ``fast_path_enabled``.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse

from .security import _url_allowed

# Bounded separately from _capture_sem: the fast path opens no browser, so it
# should not consume a browser slot, but it must not open unbounded connections
# either.
_MAX_CONCURRENT = max(1, int(os.environ.get("MANTISFETCH_NEGOTIATE_CONCURRENCY", "20")))
_sem = asyncio.Semaphore(_MAX_CONCURRENT)

_ACCEPT = "text/markdown, text/plain;q=0.9, text/html;q=0.5, */*;q=0.1"
_MARKDOWN_TYPES = ("text/markdown", "text/x-markdown", "application/markdown")
_PLAIN_TYPES = ("text/plain",)

# Bodies larger than this are not a documentation page.
_MAX_BODY_BYTES = 4 * 1024 * 1024

# agent-browser walks every ancestor. For /a/b/c/d that is five probes before the
# browser even starts, which on a page that will never hit is pure added latency.
# Two levels plus the root, issued together.
_ANCESTOR_LEVELS = 2


class NegotiationRefused(Exception):
    """The origin answered 5xx. Do not fall back to the browser and hit it again."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


@dataclass(frozen=True)
class NegotiatedDoc:
    """Markdown obtained without a browser, and where it came from."""

    markdown: str
    final_url: str
    http_status: int
    # Which rung produced it. Recorded as provenance.fetch_via — NOT as
    # provenance.source, which already means "web_capture" and is what
    # _find_capture_by_content_hash filters the index on.
    fetch_via: str


def fast_path_enabled() -> bool:
    """Whether to try negotiating at all. Off unless explicitly turned on.

    Two reasons it is not the default yet. llms.txt adoption is still thin, so
    on most pages the ladder only adds round-trips; and a negotiated capture has
    no tables, so turning it on silently changes what some captures contain.
    Turn it on, measure hit rate against real traffic, then decide.
    """
    return os.environ.get("MANTISFETCH_NEGOTIATE_MARKDOWN", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _content_type(headers: dict[str, str] | None) -> str:
    raw = ""
    for key, value in (headers or {}).items():
        if key.lower() == "content-type":
            raw = value
            break
    return raw.split(";")[0].strip().lower()


def _is_markdownish(content_type: str) -> bool:
    return content_type in _MARKDOWN_TYPES or content_type in _PLAIN_TYPES


def md_path_variant(url: str) -> str | None:
    """``/docs/x`` -> ``/docs/x.md``; ``/`` -> ``/index.md``. None if already .md."""
    parts = urlparse(url)
    path = parts.path or "/"
    if path.endswith(".md"):
        return None
    new_path = "/index.md" if path in ("", "/") else path.rstrip("/") + ".md"
    return urlunparse(parts._replace(path=new_path, query="", fragment=""))


def llms_candidates(url: str, filename: str) -> list[str]:
    """The nearest ancestor paths to probe for ``filename``, root last."""
    parts = urlparse(url)
    segments = [s for s in (parts.path or "").split("/") if s]
    prefixes: list[str] = []
    for depth in range(len(segments), 0, -1):
        prefixes.append("/" + "/".join(segments[:depth]))
        if len(prefixes) >= _ANCESTOR_LEVELS:
            break
    prefixes.append("")
    out: list[str] = []
    for prefix in prefixes:
        candidate = urlunparse(
            parts._replace(path=f"{prefix}/{filename}", query="", fragment="")
        )
        if candidate not in out:
            out.append(candidate)
    return out


_MD_LINK_RE = re.compile(r"^\s*[-*+]\s+\[([^\]]+)\]\(([^)\s]+)\)")


def find_llms_link(body: str, base_url: str, target: str) -> str | None:
    """The markdown URL an llms.txt index gives for ``target``, if any.

    Matched by path, not by link text: a title is prose and would match the
    wrong page. That makes this rung's contribution over the .md variant narrow
    but real — an index that points at the same path on a different origin, a
    docs site whose markdown is served from a CDN. Looser matching (last path
    segment, say) would reach more sites and would also confuse /a/intro with
    /b/intro, which stores the wrong document.

    Matches list-item links only — ``- [Title](/path.md)`` — which is the form
    the convention's examples use. Bare links and links under section headings
    are not read; they can be added when a site is found that needs it.

    Every link parsed out of the index is a URL the caller did not supply and
    may point anywhere, so callers must run it through the SSRF check before
    fetching it. This function only parses.
    """
    target_path = urlparse(target).path.rstrip("/") or "/"
    for line in body.splitlines():
        match = _MD_LINK_RE.match(line)
        if not match:
            continue
        href = urljoin(base_url, match.group(2))
        href_path = urlparse(href).path
        stem = href_path[:-3] if href_path.endswith(".md") else href_path
        if (stem.rstrip("/") or "/") == target_path:
            return href
    return None


async def _fetch(client, url: str, timeout_s: float) -> tuple[int, str, str] | None:
    """GET ``url``, returning (status, content_type, body) or None if unreachable.

    The SSRF check runs on every URL here — the requested one and each redirect
    hop — because this path does not go through the browser context's route
    guard, so nothing else is looking.

    Known gap, the same one ``security.py`` documents for ``_validate_url``: the
    check resolves the name and httpx resolves it again when connecting, so a
    record that changes between the two is not caught (DNS rebinding). The
    browser path closes this at the route layer by validating the request the
    browser is about to send; there is no equivalent hook for httpx short of
    pinning the resolved IP and overriding Host, which breaks TLS SNI. This is
    the residual risk the pre-check already carries, not a new one — but it is
    carried here without the route guard behind it.

    Bodies are read in chunks and abandoned past _MAX_BODY_BYTES, so the limit
    bounds what is read rather than what is kept.
    """
    headers = {"Accept": _ACCEPT, "User-Agent": "MantisFetch/negotiate"}
    for _ in range(6):  # the original plus up to five redirect hops
        if not await _url_allowed_async(url):
            return None
        try:
            async with client.stream(
                "GET", url, headers=headers, timeout=timeout_s, follow_redirects=False
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return None
                    url = urljoin(str(response.url), location)
                    continue

                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_BODY_BYTES:
                        return None
                    chunks.append(chunk)
                body = b"".join(chunks).decode(
                    response.encoding or "utf-8", errors="replace"
                )
                return response.status_code, _content_type(dict(response.headers)), body
        except Exception:
            return None
    return None


async def _url_allowed_async(url: str) -> bool:
    return await asyncio.to_thread(_url_allowed, url)


def _classify(
    result: tuple[int, str, str] | None, url: str, *, refuse_on_5xx: bool = False
) -> tuple[str, str | None, int]:
    """('hit', body, status) | ('miss', None, status) | raises for a refused 5xx.

    The status travels with the body: a 201 or 203 is a hit, and reporting it as
    200 would put a value in the manifest that the caller was told to trust.

    ``refuse_on_5xx`` is only true for the URL the caller actually asked for. The
    other rungs are speculative — a .md variant or an llms.txt that does not
    exist — and plenty of hosts answer 500 or 503 for an unknown path rather than
    404. Refusing on those would turn a page that reads perfectly well in a
    browser into a failed capture.
    """
    if result is None:
        return "miss", None, 0
    status, content_type, body = result
    if status >= 500:
        if refuse_on_5xx:
            raise NegotiationRefused(status, url)
        return "miss", None, status
    if status >= 400:
        return "miss", None, status
    if not _is_markdownish(content_type):
        return "miss", None, status
    if not body.strip():
        return "miss", None, status
    return "hit", body, status


# The requested URL gets the caller's budget; speculative probes get this. A
# hung host on three rungs would otherwise add the caller's full timeout several
# times over before the browser — which is the path a miss ends up taking anyway.
_PROBE_TIMEOUT_S = 3.0


async def try_fetch_markdown(url: str, *, timeout_ms: int = 10_000) -> NegotiatedDoc | None:
    """Walk the ladder. None when nothing answered with markdown."""
    import httpx  # noqa: PLC0415 - only on this path

    timeout_s = min(timeout_ms / 1000.0, 15.0)
    probe_s = min(_PROBE_TIMEOUT_S, timeout_s)
    async with _sem, httpx.AsyncClient() as client:
        # 1. content negotiation on the URL as given
        outcome, body, status = _classify(
            await _fetch(client, url, timeout_s), url, refuse_on_5xx=True
        )
        if outcome == "hit" and body:
            return NegotiatedDoc(body, url, status, "negotiated")

        # 2. the .md path variant
        variant = md_path_variant(url)
        if variant:
            outcome, body, status = _classify(await _fetch(client, variant, probe_s), variant)
            if outcome == "hit" and body:
                return NegotiatedDoc(body, variant, status, "md-path")

        # 3. llms.txt from the nearest ancestors, all probed together: a full
        #    miss is the common case, and probing them in sequence would only
        #    delay the browser this path is trying to avoid.
        #
        #    Only per-page links are followed. llms-full.txt is deliberately not
        #    consulted: it is the whole site in one file, so using it as the body
        #    of some particular URL stores the wrong document — and because the
        #    existing-URL guard then sees a capture for that URL, the next
        #    attempt takes the browser path and mints a second doc_id for the
        #    same page. A caller who wants llms-full.txt can request it directly,
        #    and rung 1 returns it.
        candidates = llms_candidates(url, "llms.txt")
        results = await asyncio.gather(
            *(_fetch(client, candidate, probe_s) for candidate in candidates),
            return_exceptions=True,
        )
        for candidate, result in zip(candidates, results, strict=True):
            if isinstance(result, BaseException):
                continue
            outcome, index_body, _ = _classify(result, candidate)
            if outcome != "hit" or not index_body:
                continue
            link = find_llms_link(index_body, candidate, url)
            if not link:
                continue
            # The index named this URL, which does not make it trusted: _fetch
            # runs the SSRF check on it like any other.
            outcome, body, status = _classify(await _fetch(client, link, probe_s), link)
            if outcome == "hit" and body:
                return NegotiatedDoc(body, link, status, "llms-index")

    return None

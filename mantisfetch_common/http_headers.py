"""Header values that more than one byte-serving surface has to get right.

Content-Disposition is the one so far: HTTP headers are latin-1 on the wire, so
a name with any non-latin-1 character in it cannot go in the quoted `filename=`
form at all — the server raises `UnicodeEncodeError` while encoding the
response and the caller gets a 500 (MantisFetch #294, a stored original named
`ReadyForAI业务介绍.md`).
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

__all__ = ["content_disposition"]


def content_disposition(disposition: str, filename: str) -> str:
    """Build a Content-Disposition value: ASCII fallback + RFC 5987 form.

    The `filename*` form carries the real name, percent-encoded as UTF-8, and
    is what any client that reads RFC 5987 will use. The quoted `filename=`
    stays for the ones that do not — which is why it is built by dropping the
    characters that cannot be written rather than replacing them: a client
    falling back on it saves `ReadyForAI.md`, not `ReadyForAI????.md`. A name
    with no ASCII left in its stem falls back to `source`, keeping the
    extension, so the saved file still opens with the right application.

    Control characters are stripped from the fallback so an agent-chosen
    filename carrying CR/LF cannot split or corrupt the response headers, as
    are the quote and backslash that would otherwise escape out of it. The
    `filename*` form percent-encodes all of them already.
    """
    suffix = Path(filename).suffix
    stem = filename[: -len(suffix)] if suffix else filename
    ascii_stem = stem.encode("ascii", "ignore").decode("ascii")
    ascii_stem = "".join(c for c in ascii_stem if c.isprintable() and c not in '"\\')
    ascii_suffix = suffix.encode("ascii", "ignore").decode("ascii")
    ascii_suffix = "".join(c for c in ascii_suffix if c.isprintable() and c not in '"\\')
    fallback = f"{ascii_stem or 'source'}{ascii_suffix}"
    quoted = urllib.parse.quote(filename)
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"

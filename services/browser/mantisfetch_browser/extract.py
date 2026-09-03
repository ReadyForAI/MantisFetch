"""In-process extraction of page HTML into distiller blocks.

The other two distill modes run their extraction *inside the page*: "simple"
evaluates a DOM walk, "readability" injects Readability.js first. Both pay for
that:

- Readability hands back ``article.textContent`` — plain text with the document
  structure already destroyed. Every paragraph reaches
  ``_blocks_to_sections_stable`` tagged ``p``, so its heading branch never fires
  and a long article collapses into a couple of unnamed blocks. Measured on the
  Mantis shrimp article: 54 blocks, 2 sections, no H2 anywhere, while the page
  itself has Description / Claws / Eyes / Ecology / Systematics.
- ``READABILITY_EVAL`` slices the text at 40k chars before anything downstream
  can ask for more.
- Injecting a script needs a Content-Security-Policy that permits it. Sites that
  do not (GitHub, MDN, Stack Overflow) cannot be read this way at all.

Parsing ``page.content()`` here instead removes all three at once. bs4 and lxml
already reach the image as MarkItDown transitive dependencies, but this path
needs them directly — losing them silently costs every page its heading tree —
so requirements.txt declares them.

The block vocabulary deliberately matches ``DISTILL_SIMPLE_JS`` so
``_blocks_to_sections_stable`` stays the single consumer of both paths.

What is given up: the in-page paths can ask the browser whether an element is
actually visible (``getComputedStyle`` / ``getBoundingClientRect``). Parsing the
serialized DOM cannot, so this path is more inclusive — a ``display:none``
element reaches the blocks. The tags most likely to hide content that way are
dropped outright below, and boilerplate scoring is the real answer.

What is *not* done here: tables. Converting HTML to text loses them (a Sphinx
table survives as prose, not as rows), so table extraction stays in the page
where the dedicated extractor can read cell geometry.
"""

from __future__ import annotations

import re
from typing import Any

# Structural noise: removed outright, children and all.
_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "template",
    "svg",
    "canvas",
    "form",
    "nav",
    "footer",
    "header",
    "aside",
    # A cookie/consent dialog is markup like any other here: without the page's
    # own computed styles there is no way to tell a hidden one from a shown one,
    # so it would otherwise become body text.
    "dialog",
)

# Emitted as blocks, in document order. Same set DISTILL_SIMPLE_JS queries.
_KEEP_TAGS = ("h1", "h2", "h3", "p", "li", "blockquote", "pre")
_HEADING_TAGS = frozenset({"h1", "h2", "h3"})

# A heading is worth keeping however short; body text that short is almost
# always chrome (a date stamp, a "Share" label, a nav crumb).
_MIN_BODY_CHARS = 20

_MAX_BLOCKS = 1500

# A ceiling on what one capture writes, not a content budget: two orders of
# magnitude above any real article (the largest measured was 45k), and there
# purely so a pathological page — a minified blob inside <pre>, an infinite
# feed — cannot write without bound. If this ever truncates a real page, raise
# it; do not reach for the display budget instead.
_MAX_TOTAL_CHARS = 2_000_000

# Permalink affordances that render inside the heading and would otherwise become
# part of the sid title: Sphinx's pilcrow ("Basic Usage\u00b6") and MediaWiki's
# section edit link ("Selected extant species[edit]"). Both were visible in
# captured section titles.
_HEADING_NOISE_RE = re.compile(r"(?:\s*\[edit\]|\s*[\u00b6\u00a7])+\s*$")


def _clean_text(node: Any) -> str:
    """Node text with runs of whitespace collapsed."""
    return " ".join(node.get_text(" ", strip=True).split())


def html_to_blocks(
    html: str, max_blocks: int = _MAX_BLOCKS, max_chars: int = _MAX_TOTAL_CHARS
) -> list[dict[str, str]]:
    """Parse page HTML into ``[{"tag": ..., "text": ...}]`` distiller blocks.

    Returns an empty list when the HTML yields nothing usable, which the caller
    treats as a signal to fall back to the in-page simple distiller.
    """
    if not html:
        return []
    from bs4 import BeautifulSoup  # noqa: PLC0415 - heavy import, only on this path

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        # lxml is a declared dependency, so reaching here means a broken install
        # rather than a missing extra. Falling straight through to "simple" would
        # quietly cost every page its heading tree, so try the stdlib parser
        # first and only give up if that fails too.
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []

    body = soup.body or soup
    for tag in body.find_all(_DROP_TAGS):
        tag.decompose()

    blocks: list[dict[str, str]] = []
    seen_nested: set[int] = set()
    total_chars = 0
    for node in body.find_all(_KEEP_TAGS):
        # A <li> holding a <p>, or a <blockquote> holding both, would otherwise
        # contribute its text twice — once as the container and again as each
        # child. The outermost wins: it is emitted whole and its descendants are
        # marked seen, which keeps a list item's text in one block.
        if id(node) in seen_nested:
            continue
        for descendant in node.find_all(_KEEP_TAGS):
            seen_nested.add(id(descendant))

        text = _clean_text(node)
        if not text:
            continue
        tag = node.name
        if tag in _HEADING_TAGS:
            text = _HEADING_NOISE_RE.sub("", text)
            if not text:
                continue
        elif len(text) < _MIN_BODY_CHARS:
            continue
        blocks.append({"tag": tag, "text": text})
        total_chars += len(text)
        if len(blocks) >= max_blocks or total_chars >= max_chars:
            break

    return blocks


def html_title(html: str) -> str | None:
    """The document's <title>, or None when it has none."""
    if not html:
        return None
    from bs4 import BeautifulSoup  # noqa: PLC0415

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return None
    if soup.title and soup.title.string:
        return " ".join(soup.title.string.split()) or None
    return None

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
dropped outright below, and the boilerplate pruning that follows them covers the
chrome that does not announce itself with a tag.

What is *not* done here: tables. Converting HTML to text loses them (a Sphinx
table survives as prose, not as rows), so table extraction stays in the page
where the dedicated extractor can read cell geometry.
"""

from __future__ import annotations

import math
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

# ── boilerplate pruning ────────────────────────────────────────────────────────
# Dropping nav/footer/aside by tag only catches chrome that says what it is.
# Everything else has to be scored. Measured leftovers on real pages:
#
#   Wikipedia tail  "CS1 maint: publisher location", "Articles containing
#                   Chinese-language text" — hidden maintenance categories, an
#                   unmarked <div> of nothing but links
#   Sphinx head/tail "3.14.7 Documentation »", "The Python Standard Library »"
#                   — a breadcrumb rendered as <li>, and emitted twice
#
# Both are ~100% link text, which is what makes link density the load-bearing
# signal. Hardcoding .navbox / .related instead would fix these two sites and no
# others.
_PRUNE_CONTAINERS = ("div", "section", "ul", "ol", "dl")

# Names that describe chrome. Required before anything is removed: link density
# alone deletes real content. Measured on the Mantis shrimp page, the species
# list (Archaeocaris vermiformis, Gorgonophontes fraiponti, ...) is a list where
# every item is a link, so it scores exactly like a navigation block — and it is
# the article. Density decides *whether* a self-declared chrome container really
# is one; it cannot nominate a container on its own.
# Matched on word boundaries against the class/id text. Without them "share"
# hits "shareholder" and "nav" hits anything containing those three letters, so
# each form that should match is spelled out instead — "navigation" is listed
# because \bnav\b does not reach it.
_CHROME_NAME_RE = re.compile(
    r"\b(?:nav|navigation|navbar|navbox|footer|header|sidebar|side-?bar|menu|"
    r"breadcrumb|breadcrumbs|crumb|toc|catlinks|advert|advertisement|promo|"
    r"social|share|related|banner|cookie|consent|subscribe|newsletter)\b",
    re.I,
)

# Below this a self-declared chrome container really is chrome. It exists to
# spare the container that names itself "related" or "sidebar" and then holds
# actual prose — a name is a hint about intent, not proof about content.
_PRUNE_THRESHOLD = 0.35

# A container this small cannot be judged by density — a one-line paragraph and
# a one-line breadcrumb look identical. Left for the block-level filters.
_PRUNE_MIN_TEXT = 40


# Containers whose name is the site *asserting there is no content here*: an
# empty state, a failed load, a loading skeleton. Unlike the chrome vocabulary
# above these are removed without a density check, because the thing they hold
# is prose — GitHub's blankslate reads "Uh oh! There was an error while loading",
# which scores as content by every density measure and is not content by any
# other.
#
# Kept deliberately short, because removing without corroboration is only safe
# for words that mean one thing. "placeholder" and "skeleton" are both out:
# a loading skeleton carries almost no text and the 40-character floor below
# already drops it, while an article about anatomy may legitimately name a
# container either word.
_EMPTY_STATE_RE = re.compile(r"\b(?:blankslate|empty-?state)\b", re.I)


def _node_names(node: Any) -> str:
    attrs = node.attrs or {}
    names = attrs.get("class") or []
    if isinstance(names, str):
        names = [names]
    return " ".join([*names, str(attrs.get("id") or "")])


def _chrome_name_hit(node: Any) -> bool:
    """True when the node's class or id names it as chrome."""
    haystack = _node_names(node)
    return bool(haystack.strip()) and bool(_CHROME_NAME_RE.search(haystack))


def _content_score(node: Any, text_len: int) -> float:
    """How much this container looks like content rather than chrome, in [0, 1].

    Three signals, none of which is site-specific:

    - link density — the share of the text sitting inside <a>. A navigation
      block is nearly all links; prose is nearly none. This dominates.
    - text density — text length against markup length. Chrome is many small
      elements around little text.
    - bulk — a long container is unlikely to be chrome whatever else it looks
      like, so length can rescue a link-heavy but substantial node.
    """
    if not text_len:
        return 0.0
    # decode_contents() skips the node's own tag and, unlike str(node), does not
    # re-serialize it for every candidate on a div-heavy page.
    markup_len = len(node.decode_contents()) or 1
    link_text_len = sum(len(a.get_text(" ", strip=True)) for a in node.find_all("a"))

    link_density = min(1.0, link_text_len / text_len)
    text_density = min(1.0, text_len / markup_len)
    bulk = min(1.0, math.log10(text_len + 1) / 4.0)  # 10k chars saturates

    return 0.55 * (1.0 - link_density) + 0.25 * text_density + 0.20 * bulk


def _prune_boilerplate(body: Any) -> None:
    """Remove containers that name themselves chrome and read like it."""
    for node in list(body.find_all(_PRUNE_CONTAINERS)):
        if node.parent is None:  # already removed along with an ancestor
            continue
        _prune_node(node)


def _prune_node(node: Any) -> None:
    """Remove one container if it both names itself chrome and reads like it."""
    if node.parent is None:
        return
    # An empty state goes without corroboration: the density check would keep it,
    # since the placeholder text reads exactly like prose.
    if _EMPTY_STATE_RE.search(_node_names(node)):
        node.decompose()
        return
    if not _chrome_name_hit(node):
        return
    text_len = len(node.get_text(" ", strip=True))
    if text_len < _PRUNE_MIN_TEXT:
        return
    if _content_score(node, text_len) < _PRUNE_THRESHOLD:
        node.decompose()


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
    _prune_boilerplate(body)

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


# ── markdown ───────────────────────────────────────────────────────────────────
# The negotiated fetch path receives markdown, not HTML, but everything
# downstream — sections, sids, content_hash — must not care which arrived. Both
# converge here on the same block vocabulary, so one page cannot get one identity
# from one path and a different identity from the other.

_ATX_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def markdown_to_blocks(md: str, max_blocks: int = _MAX_BLOCKS) -> list[dict[str, str]]:
    """Parse markdown into the same blocks ``html_to_blocks`` produces.

    Line-based on purpose. A full CommonMark parser would be a new dependency
    and would normalize away the structure this exists to keep; what arrives
    from a .md endpoint or an llms.txt link is ATX headings, paragraphs, fenced
    code and lists.

    Heading levels below h3 collapse to h3: ``_blocks_to_sections_stable`` only
    breaks on h1-h3, so emitting h4 would silently make it body text.
    """
    if not md:
        return []
    blocks: list[dict[str, str]] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        text = " ".join(" ".join(paragraph).split())
        paragraph.clear()
        if len(text) >= _MIN_BODY_CHARS:
            blocks.append({"tag": "p", "text": text})

    lines = md.splitlines()
    i = 0
    while i < len(lines) and len(blocks) < max_blocks:
        line = lines[i]

        if _FENCE_RE.match(line):
            # Atomic: a fence's interior is code, and its indentation is the
            # content. Collected verbatim rather than whitespace-collapsed.
            flush_paragraph()
            body: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE_RE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence
            code = "\n".join(body).strip("\n")
            if code.strip():
                blocks.append({"tag": "pre", "text": code})
            continue

        heading = _ATX_RE.match(line)
        if heading:
            flush_paragraph()
            level = min(len(heading.group(1)), 3)
            title = heading.group(2).strip().rstrip("#").strip()
            if title:
                blocks.append({"tag": f"h{level}", "text": title})
            i += 1
            continue

        item = _LIST_RE.match(line)
        if item:
            flush_paragraph()
            text = " ".join(item.group(1).split())
            if len(text) >= _MIN_BODY_CHARS:
                blocks.append({"tag": "li", "text": text})
            i += 1
            continue

        if not line.strip():
            flush_paragraph()
        else:
            paragraph.append(line.strip())
        i += 1

    if len(blocks) < max_blocks:
        flush_paragraph()
    return blocks[:max_blocks]

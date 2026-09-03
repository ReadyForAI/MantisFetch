"""Shared utilities across MantisFetch services (browser + docreader).

Houses code that must stay byte-identical between the /web and /doc services —
chiefly the document-library storage layout that doc-index v2 shares across
both. Lives at the repo root (alongside ``i18n`` and ``providers``) so it is
importable without any ``sys.path`` changes.
"""

#: Single source of truth for the MantisFetch version. Every surface that
#: reports a version (``/health``, both FastAPI apps, the MCP ``serverInfo``)
#: reads this, and ``pyproject.toml`` picks it up via ``dynamic = ["version"]``
#: — the literal must not be repeated anywhere else.
__version__ = "1.7.3"

"""Shared utilities across MantisFetch services (browser + docreader).

Houses code that must stay byte-identical between the /web and /doc services —
chiefly the document-library storage layout that doc-index v2 shares across
both. Lives at the repo root (alongside ``i18n`` and ``providers``) so it is
importable without any ``sys.path`` changes.
"""

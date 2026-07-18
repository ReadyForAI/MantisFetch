"""Per-document lowercase text cache for ``/library/search_text`` (B2).

Built at parse/persist time so queries avoid re-reading and re-lowercasing every
``full.md`` / section file on each request. Cache files live under
``{doc_dir}/.cache/`` and are safe to delete (search falls back to live files).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FULL_CACHE = "search_full.lower.txt"
_SECTIONS_CACHE = "search_sections.lower.json"


def write_search_cache(
    doc_dir: Path,
    *,
    full_text: str | None = None,
    sections: list[dict[str, Any]] | None = None,
    doc_id: str | None = None,
    docs_dir: Path | None = None,
) -> None:
    """Write lowercase full-text and optional section cache entries.

    ``sections`` items: ``{sid, title, text, file?, page_range?, page_start?, page_end?}``.
    When ``doc_id`` + ``docs_dir`` are provided, also updates the SQLite FTS index (B3).
    """
    cache_dir = doc_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    body_parts: list[str] = []
    if full_text is not None:
        (cache_dir / _FULL_CACHE).write_text(full_text.lower(), encoding="utf-8")
        body_parts.append(full_text)
    if sections is not None:
        payload = []
        for sec in sections:
            text = sec.get("text") or ""
            title = sec.get("title") or ""
            payload.append(
                {
                    "sid": sec.get("sid"),
                    "title": title,
                    "title_lower": title.lower(),
                    "text_lower": text.lower(),
                    "file": sec.get("file"),
                    "page_range": sec.get("page_range"),
                    "page_start": sec.get("page_start"),
                    "page_end": sec.get("page_end"),
                }
            )
            body_parts.append(title)
            body_parts.append(text)
        (cache_dir / _SECTIONS_CACHE).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    if doc_id and docs_dir is not None:
        try:
            from mantisfetch_common import doc_index_store as dis

            dis.upsert_fts(docs_dir, doc_id, "\n".join(body_parts))
        except Exception:
            pass


def read_full_lower(doc_dir: Path) -> str | None:
    path = doc_dir / ".cache" / _FULL_CACHE
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def read_sections_lower(doc_dir: Path) -> list[dict[str, Any]] | None:
    path = doc_dir / ".cache" / _SECTIONS_CACHE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def invalidate_search_cache(doc_dir: Path) -> None:
    """Remove search cache files so search falls back to live full/section files."""
    cache_dir = doc_dir / ".cache"
    for name in (_FULL_CACHE, _SECTIONS_CACHE):
        path = cache_dir / name
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

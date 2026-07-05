"""Document-library storage layout shared by the browser and docreader services.

doc-index v2 stores web captures (/web) and uploaded documents (/doc) under one
root, so the path and content-type helpers below must resolve identically for
both services. They were previously duplicated byte-for-byte in each service
module; this is the single source of truth.
"""

import os
import threading
from pathlib import Path
from typing import Any

from fastapi import HTTPException

# Single lock guarding read-modify-write of the shared doc-index.json. Both the
# /web (browser) and /doc (docreader) sub-apps run in one process and update the
# same index file, so they must serialize through ONE lock — previously each had
# its own, allowing lost updates when a capture and a parse wrote concurrently.
_doc_index_lock = threading.Lock()

DEFAULT_DOCS_DIR = Path(
    os.environ.get(
        "MANTISFETCH_DOCS_DIR",
        os.path.expanduser("~/.mantisfetch/docs"),
    )
)

CONTENT_TYPE_DIRS = ("General", "Contract", "Bid", "Knowledge")
_CONTENT_TYPE_ALIASES = {name.lower(): name for name in CONTENT_TYPE_DIRS}


def _get_docs_dir() -> Path:
    """Return the document library root, creating it if necessary."""
    d = DEFAULT_DOCS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalize_content_type(value: str | None) -> str:
    raw = (value or "General").strip()
    normalized = _CONTENT_TYPE_ALIASES.get(raw.lower())
    if not normalized:
        allowed = ", ".join(CONTENT_TYPE_DIRS)
        raise HTTPException(422, f"content_type must be one of: {allowed}")
    return normalized


def _doc_storage_rel_path(doc_id: str, content_type: str | None = None) -> str:
    if content_type is None:
        return doc_id
    return f"{_normalize_content_type(content_type)}/{doc_id}"


def _doc_storage_dir(docs_dir: Path, doc_id: str, content_type: str | None = None) -> Path:
    return docs_dir / _doc_storage_rel_path(doc_id, content_type)


def _doc_manifest_exists_anywhere(docs_dir: Path, doc_id: str) -> bool:
    """True if a document with this id already has a manifest on disk, whether in
    the flat layout or under any content-type directory.

    Used by both /doc and /web id allocation so a counter mint can't silently
    overwrite an existing document (e.g. after a counter file is reset).
    """
    if (docs_dir / doc_id / "manifest.json").exists():
        return True
    return any((docs_dir / ct / doc_id / "manifest.json").exists() for ct in CONTENT_TYPE_DIRS)


def _indexable_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only shallow scalar metadata in doc-index for cheap filtering.

    Both /web (browser captures) and /doc (uploaded documents) write into the
    shared doc-index, so this lives here as the single source of truth for what
    metadata is index-filterable.
    """
    out: dict[str, Any] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(raw, (str, int, float, bool)) or raw is None:
            out[key] = raw
        elif isinstance(raw, list) and all(
            isinstance(item, (str, int, float, bool)) or item is None for item in raw
        ):
            out[key] = raw[:20]
    return out

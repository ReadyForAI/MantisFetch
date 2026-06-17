"""Docreader-side storage: doc_id reservation, doc-index read/write, doc-dir resolution.

This module owns the on-disk bookkeeping for parsed documents:

- doc_id minting/reservation (`_next_doc_id`, `_resolve_doc_id` and the
  per-doc_id parse locks that serialize concurrent pins of the same id),
- the doc-index.json read/write layer (`_load_doc_index`, `_update_doc_index`),
- doc directory resolution from index/manifest (`_resolve_doc_dir` and friends).

The cross-service path/content-type primitives live in `mantisfetch_common.storage`;
this layer is docreader-specific (DOC-prefixed ids, doc-index schema, locks).
Locks and the WeakValueDictionary are process-local state and stay here — they
are re-exported from the package facade as shared references, not duplicated.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import threading
import weakref
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from i18n import t
from mantisfetch_common.atomic import _write_json
from mantisfetch_common.storage import (
    CONTENT_TYPE_DIRS,
    _doc_index_lock,
    _doc_storage_dir,
    _doc_storage_rel_path,
    _normalize_content_type,
)

# ═══════════════════════════════════════════
# Per-doc_id parse locks
# ═══════════════════════════════════════════

# Per-doc_id locks serialize concurrent /doc/parse requests that pin the same
# explicit doc_id, so the existence check + write reservation can't race past
# each other when _MAX_CONCURRENT_PARSE > 1.
#
# WeakValueDictionary so entries vanish once no request still references the
# Lock — long-running servers receiving high-cardinality explicit ids would
# otherwise leak one Lock per id forever. While requests are queued on a
# lock their `async with lock:` frame keeps it alive.
_doc_id_parse_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_doc_id_parse_locks_guard = asyncio.Lock()


@contextlib.asynccontextmanager
async def _optional_doc_id_lock(doc_id: str | None):
    """Hold a per-doc_id lock for the duration of the parse when doc_id is pinned."""
    if not doc_id:
        yield
        return
    async with _doc_id_parse_locks_guard:
        lock = _doc_id_parse_locks.get(doc_id)
        if lock is None:
            lock = asyncio.Lock()
            _doc_id_parse_locks[doc_id] = lock
    async with lock:
        yield


# ═══════════════════════════════════════════
# Document index
# ═══════════════════════════════════════════

_doc_counter_lock = threading.Lock()
# _doc_index_lock is the process-wide shared lock from mantisfetch_common.storage
# (imported above) so /web and /doc serialize on the same doc-index.json.


def _indexable_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only shallow scalar metadata in doc-index for cheap filtering."""
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


def _update_doc_index(
    docs_dir: Path,
    meta: dict,
    digest: str,
    tags: list[str] | None = None,
    source: str = "upload",
    source_url: str | None = None,
    content_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
    source_record: dict[str, Any] | None = None,
    content_type: str | None = None,
    storage_path: str | None = None,
):
    """Update doc-index.json with threading lock and atomic write."""
    with _doc_index_lock:
        index_path = docs_dir / "doc-index.json"
        if index_path.exists():
            try:
                with open(index_path, encoding="utf-8") as f:
                    index = json.load(f)
            except (OSError, ValueError):
                index = {"version": 2, "documents": []}
        else:
            index = {"version": 2, "documents": []}

        index["version"] = 2
        if not isinstance(index.get("documents"), list):
            index["documents"] = []
        index["documents"] = [d for d in index["documents"] if d.get("id") != meta["doc_id"]]
        normalized_content_type = _normalize_content_type(
            content_type or meta.get("content_type") or "General"
        )
        rel_storage_path = storage_path or meta.get("storage_path") or _doc_storage_rel_path(
            meta["doc_id"],
            normalized_content_type if content_type or meta.get("storage_path") else None,
        )

        entry: dict[str, Any] = {
            "id": meta["doc_id"],
            "filename": meta["filename"],
            "file_type": meta["file_type"],
            "content_type": normalized_content_type,
            "storage_path": rel_storage_path,
            "source": source,
            "source_url": source_url or "",
            "pages": meta["total_pages"],
            "sections": meta["section_count"],
            "ocr_pages": meta.get("ocr_page_count", 0),
            "tables": meta.get("table_count", 0),
            "digest": digest[:200],
            "digest_path": f"docs/{rel_storage_path}/digest.md",
            "tags": tags or [],
            "created_at": meta["created_at"],
            "content_hash": content_hash or "",
            "metadata": _indexable_metadata(metadata or meta.get("metadata") or {}),
            "source_ref": (source_record or meta.get("source_file") or {}).get("ref", ""),
            "source_filename": (source_record or meta.get("source_file") or {}).get("filename", ""),
            "source_sha256": (source_record or meta.get("source_file") or {}).get("sha256", ""),
            "source_available": bool((source_record or meta.get("source_file") or {}).get("ref")),
        }
        summary_meta = (
            meta.get("parse_metadata", {}).get("summary")
            if isinstance(meta.get("parse_metadata"), dict)
            else {}
        )
        if isinstance(summary_meta, dict):
            entry["summary_mode"] = summary_meta.get("mode")
            entry["summary_status"] = summary_meta.get("status")
            entry["summary_error_code"] = summary_meta.get("error_code")

        index["documents"].append(entry)
        index["last_updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_json(index_path, index)


def _load_doc_index(docs_dir: Path) -> list[dict[str, Any]]:
    index_path = docs_dir / "doc-index.json"
    if not index_path.exists():
        return []
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    documents = index.get("documents", [])
    return documents if isinstance(documents, list) else []


def _load_doc_tags(docs_dir: Path, doc_id: str) -> list[str]:
    for entry in _load_doc_index(docs_dir):
        if entry.get("id") == doc_id:
            tags = entry.get("tags")
            if isinstance(tags, list):
                return [str(tag) for tag in tags]
            return []
    return []


# ═══════════════════════════════════════════
# doc_id minting / reservation
# ═══════════════════════════════════════════

_DOC_ID_RE = re.compile(r"^(?=.{1,80}$)(?=.*\d)[A-Za-z0-9](?:[A-Za-z0-9-]{0,78}[A-Za-z0-9])?$")


def _validate_doc_id(doc_id: str) -> None:
    """Reject doc_id values that could cause path traversal."""
    if not _DOC_ID_RE.match(doc_id):
        raise HTTPException(400, f"invalid doc_id: {doc_id!r}")


def _next_doc_id(docs_dir: Path) -> str:
    with _doc_counter_lock:
        counter_path = docs_dir / ".counter"
        if counter_path.exists():
            try:
                counter = int(counter_path.read_text(encoding="utf-8").strip())
            except ValueError:
                counter = 1
        else:
            counter = 1
        # Skip ids that already exist on disk (e.g. .counter was reset, or a doc
        # was previously created with an explicit DOC-NNN id) so a counter mint
        # can never silently overwrite an existing document. Raise rather than
        # return a colliding id if the search space is somehow exhausted.
        for _ in range(1_000_000):
            doc_id = f"DOC-{counter:03d}"
            counter += 1
            if not _doc_exists_anywhere(docs_dir, doc_id):
                break
        else:
            raise RuntimeError("doc_id allocation exhausted: too many existing DOC ids")
        counter_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = counter_path.with_suffix(".tmp")
        tmp.write_text(str(counter), encoding="utf-8")
        os.replace(tmp, counter_path)
        return doc_id


def _doc_id_strategy(requested_strategy: str | None = None) -> str:
    strategy = (requested_strategy or os.environ.get("MANTISFETCH_DOC_ID_STRATEGY", "counter")).strip().lower()
    return strategy if strategy in {"counter", "source_filename"} else "counter"


def _sanitize_doc_id_candidate(value: str, max_len: int = 80) -> str:
    base = Path(value).name.strip()
    stem = Path(base).stem if Path(base).suffix else base
    normalized = re.sub(r"[\s._]+", "-", stem)
    sanitized = re.sub(r"[^A-Za-z0-9-]+", "", normalized)
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-")
    return sanitized[:max_len]


def _next_filename_doc_id(docs_dir: Path, filename: str) -> str | None:
    base = _sanitize_doc_id_candidate(filename)
    if not base:
        return None
    candidate = base
    suffix = 2
    # `candidate in _doc_id_parse_locks` filters out ids that another concurrent
    # parse has reserved but not yet written a manifest for, preventing two
    # same-filename uploads from racing past this check and both choosing the
    # same id. Caller must hold `_doc_id_parse_locks_guard` so the check + the
    # subsequent insert in the dict are atomic.
    while _doc_exists_anywhere(docs_dir, candidate) or candidate in _doc_id_parse_locks:
        # Reserve room for "-<suffix>" inside the 80-char limit so we always
        # produce a candidate distinct from `base`. Without this, an 80-char
        # `base` that's already reserved would loop forever — `f"{base}-2"[:80]`
        # is just `base`, leaving the candidate unchanged.
        suffix_str = f"-{suffix}"
        head_len = max(1, 80 - len(suffix_str))
        next_candidate = (base[:head_len] + suffix_str).rstrip("-")
        if not next_candidate or next_candidate == candidate:
            return None
        candidate = next_candidate
        suffix += 1
        if suffix > 10000:
            return None
    return candidate if _DOC_ID_RE.match(candidate) else None


def _resolve_doc_id(
    docs_dir: Path,
    filename: str,
    requested_doc_id: str | None,
    requested_strategy: str | None = None,
) -> str:
    if requested_doc_id:
        _validate_doc_id(requested_doc_id)
        return requested_doc_id

    if _doc_id_strategy(requested_strategy) == "source_filename":
        filename_doc_id = _next_filename_doc_id(docs_dir, filename)
        if filename_doc_id:
            return filename_doc_id

    return _next_doc_id(docs_dir)


# ═══════════════════════════════════════════
# doc directory resolution
# ═══════════════════════════════════════════


def _resolve_index_storage_path(docs_dir: Path, storage_path: Any) -> Path | None:
    if not isinstance(storage_path, str) or not storage_path.strip():
        return None
    raw_path = Path(storage_path)
    if raw_path.is_absolute() or ".." in raw_path.parts:
        return None
    candidate = (docs_dir / raw_path).resolve()
    try:
        candidate.relative_to(docs_dir.resolve())
    except ValueError:
        return None
    return candidate


def _find_doc_index_entry(docs_dir: Path, doc_id: str) -> dict[str, Any] | None:
    for entry in _load_doc_index(docs_dir):
        if entry.get("id") == doc_id:
            return entry
    return None


def _resolve_doc_dir(docs_dir: Path, doc_id: str) -> Path:
    _validate_doc_id(doc_id)
    entry = _find_doc_index_entry(docs_dir, doc_id)
    if entry:
        indexed_path = _resolve_index_storage_path(docs_dir, entry.get("storage_path"))
        if indexed_path and (indexed_path / "manifest.json").exists():
            return indexed_path
        indexed_type = entry.get("content_type")
        if isinstance(indexed_type, str):
            typed_path = _doc_storage_dir(docs_dir, doc_id, indexed_type)
            if (typed_path / "manifest.json").exists():
                return typed_path

    for content_type in CONTENT_TYPE_DIRS:
        typed_path = _doc_storage_dir(docs_dir, doc_id, content_type)
        if (typed_path / "manifest.json").exists():
            return typed_path

    legacy_path = docs_dir / doc_id
    if (legacy_path / "manifest.json").exists():
        return legacy_path
    raise HTTPException(404, t("doc_not_found", doc_id=doc_id))


def _doc_exists_anywhere(docs_dir: Path, doc_id: str) -> bool:
    try:
        _resolve_doc_dir(docs_dir, doc_id)
        return True
    except HTTPException as exc:
        if exc.status_code == 404:
            return False
        raise


def _doc_content_type(docs_dir: Path, doc_id: str) -> str:
    # Derive from the on-disk doc directory first — that's the authoritative
    # location. doc-index.json may carry a stale content_type that points at a
    # directory that no longer holds the manifest, and trusting it would let
    # `replace=true` write the new artifacts under the wrong category dir
    # (orphaning the real files).
    try:
        doc_dir = _resolve_doc_dir(docs_dir, doc_id)
    except HTTPException:
        return "General"
    try:
        rel_parts = doc_dir.relative_to(docs_dir).parts
    except ValueError:
        rel_parts = ()
    if len(rel_parts) >= 2 and rel_parts[0] in CONTENT_TYPE_DIRS:
        return rel_parts[0]
    # Legacy flat layout (or unrecognized prefix): consult the manifest, then
    # the index, then default to General.
    manifest_path = doc_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest, dict) and isinstance(manifest.get("content_type"), str):
                return _normalize_content_type(manifest.get("content_type"))
        except Exception:
            pass
    entry = _find_doc_index_entry(docs_dir, doc_id)
    if entry and isinstance(entry.get("content_type"), str):
        try:
            return _normalize_content_type(entry.get("content_type"))
        except HTTPException:
            pass
    return "General"


def _doc_entry_from_manifest(docs_dir: Path, doc_id: str) -> dict[str, Any] | None:
    try:
        doc_dir = _resolve_doc_dir(docs_dir, doc_id)
    except HTTPException:
        return None
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(manifest, dict):
        return None

    meta: dict[str, Any] = {}
    meta_path = doc_dir / ".meta.json"
    if meta_path.exists():
        try:
            raw_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(raw_meta, dict):
                meta = raw_meta
        except Exception:
            meta = {}

    source_file = manifest.get("source_file") or meta.get("source_file") or {}
    provenance = manifest.get("provenance") or {}
    content_type = _normalize_content_type(manifest.get("content_type") or meta.get("content_type") or "General")
    storage_path = str(manifest.get("storage_path") or meta.get("storage_path") or doc_dir.relative_to(docs_dir))
    sections = manifest.get("sections") if isinstance(manifest.get("sections"), list) else []
    images = manifest.get("images") if isinstance(manifest.get("images"), list) else []
    manifest_tags = manifest.get("tags") if isinstance(manifest.get("tags"), list) else None
    parse_metadata = manifest.get("parse_metadata") if isinstance(manifest.get("parse_metadata"), dict) else {}
    summary_meta = parse_metadata.get("summary") if isinstance(parse_metadata.get("summary"), dict) else {}
    digest = ""
    digest_path = doc_dir / "digest.md"
    if digest_path.exists():
        try:
            digest = digest_path.read_text(encoding="utf-8")[:200]
        except Exception:
            digest = ""

    return {
        "id": doc_id,
        "filename": manifest.get("filename") or meta.get("filename") or "",
        "file_type": manifest.get("file_type") or meta.get("file_type") or "",
        "content_type": content_type,
        "storage_path": storage_path,
        "source": manifest.get("source") or provenance.get("source") or "upload",
        "source_url": provenance.get("source_url") or "",
        "pages": meta.get("total_pages", 0),
        "sections": len(sections),
        "ocr_pages": meta.get("ocr_page_count", 0),
        "tables": meta.get("table_count", 0),
        "images": len(images) if images else meta.get("image_count", 0),
        "digest": digest,
        "digest_path": f"docs/{storage_path}/digest.md",
        "tags": manifest_tags if manifest_tags is not None else meta.get("tags", []),
        "created_at": provenance.get("created_at") or meta.get("created_at"),
        "content_hash": provenance.get("content_hash") or "",
        "metadata": _indexable_metadata(manifest.get("metadata") or meta.get("metadata") or {}),
        "source_ref": source_file.get("ref", ""),
        "source_filename": source_file.get("filename", ""),
        "source_sha256": source_file.get("sha256", ""),
        "source_available": bool(source_file.get("ref")),
        "summary_mode": summary_meta.get("mode"),
        "summary_status": summary_meta.get("status"),
        "summary_error_code": summary_meta.get("error_code"),
    }

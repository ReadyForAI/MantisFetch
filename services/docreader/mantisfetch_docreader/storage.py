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
import logging
import os
import re
import shutil
import threading
import weakref
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from i18n import t
from mantisfetch_common.storage import (
    CONTENT_TYPE_DIRS,
    _doc_index_lock,
    _doc_storage_dir,
    _doc_storage_rel_path,
    _indexable_metadata,
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


logger = logging.getLogger("mantisfetch_docreader")


def _export_index_json(docs_dir: Path) -> None:
    """Refresh ``doc-index.json`` from the database, best effort.

    The commit already happened. This file is a derived view, and readers prefer
    the database, so a failure here leaves the export stale rather than the
    library wrong — and the next successful write rewrites it whole. Raising
    instead would be worse than the bug it looks like it is preventing: the
    caller's rollback puts the *files* back but cannot un-commit the row, so a
    replacement would end up with the old document on disk and the new one's
    metadata in the index.
    """
    from mantisfetch_common import doc_index_store as dis

    try:
        dis.export_json(
            docs_dir, last_updated=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except Exception as exc:  # noqa: BLE001 - the index is committed either way
        logger.warning("doc-index.json export failed (index is committed): %s", exc)


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
    kind: str | None = None,
):
    """Commit one document to the index.

    SQLite is the commit point and ``doc-index.json`` is its export. Both are
    written here, in that order, because the export is a full rewrite from the
    database: a record that reaches JSON but not SQLite is erased by the next
    successful write, and a delete that reaches only JSON is undone the same way.
    So a write that cannot commit raises rather than leaving one behind — the
    callers on this path (parse, capture) all roll their files back and record
    the failure, which a silently-unindexed document on disk cannot be.
    """
    with _doc_index_lock:
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
        # Only raw documents carry it, so an existing row keeps meaning what it
        # meant: absent is parsed. Search hits need it because "find it, then
        # read its digest" is the standard flow, and a raw document has no
        # digest to read.
        if kind == "raw":
            entry["kind"] = "raw"
        summary_meta = (
            meta.get("parse_metadata", {}).get("summary")
            if isinstance(meta.get("parse_metadata"), dict)
            else {}
        )
        if isinstance(summary_meta, dict):
            entry["summary_mode"] = summary_meta.get("mode")
            entry["summary_status"] = summary_meta.get("status")
            entry["summary_error_code"] = summary_meta.get("error_code")

        from mantisfetch_common import doc_index_store as dis

        dis.upsert_document(docs_dir, entry)
        _export_index_json(docs_dir)


def _load_doc_index(docs_dir: Path) -> list[dict[str, Any]]:
    try:
        from mantisfetch_common import doc_index_store as dis

        # An empty result is an answer, not a miss: the database is the index of
        # record, and "no documents" is what an empty library looks like. Reading
        # the JSON export instead would resurrect whatever the last delete
        # removed on any run where the export did not get written.
        return dis.list_documents(docs_dir)
    except Exception:
        pass
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


#: What a product directory is renamed to while its delete commits: a sibling,
#: so the rename is atomic, and not a legal doc_id (the id pattern has no "."),
#: so nothing resolves it as a document. On disk it is also the record that a
#: delete was in flight — see _finish_interrupted_deletes.
_DELETING_SUFFIX = ".deleting"


def _put_back_set_aside(set_aside: list[tuple[Path, Path]]) -> None:
    """Undo the renames of a delete that did not commit. Never raises: this runs
    on the way out of a failure, and the startup sweep restores anything left."""
    for original, tombstone in reversed(set_aside):
        try:
            os.replace(tombstone, original)
        except OSError as exc:
            logger.error(
                "Could not put %s back after a failed delete; it stays at %s and is "
                "restored at the next start: %s",
                original,
                tombstone,
                exc,
            )


def _delete_doc(docs_dir: Path, doc_id: str) -> bool:
    """Remove a document's doc-index entry and all on-disk products, serialized on
    the shared doc-index lock so it can't race a concurrent capture/parse index
    write. Idempotent: returns True if anything existed and was removed, False if
    the doc_id was absent everywhere — callers treat both as success.

    The index commit decides whether the delete happened, and the products follow
    it either way. They are renamed aside first (``{doc_id}.deleting``), the row
    is deleted, and only then are they removed. A commit that fails renames them
    back, so the caller is told the delete failed about a document that is still
    whole — removing them first left an index row pointing at nothing, which no
    retry, restore or export could repair. A cleanup that fails after the commit
    is logged, not raised: the document is gone as far as any reader can tell,
    and the leftover directory is cleared at the next start. So is a delete the
    process died in the middle of (_finish_interrupted_deletes).

    The removal set is the index entry's own resolved storage_path (covers
    migrated/legacy layouts where it isn't one of the current content-type dirs)
    plus every known content-type dir and the legacy flat path. doc_id is
    validated by the caller (`_validate_doc_id`), so joins can't escape.
    """
    with _doc_index_lock:
        entry = _find_doc_index_entry(docs_dir, doc_id)
        product_dirs: list[Path] = []
        if entry is not None:
            resolved = _resolve_index_storage_path(docs_dir, entry.get("storage_path"))
            # Only trust the indexed path if it actually names THIS doc's product
            # dir (…/{doc_id}). A malformed/stale storage_path like "General" or "."
            # stays inside docs_dir but resolves to a whole content-type dir or the
            # docs root — rmtree'ing it would wipe unrelated documents. The known
            # layout candidates below are built as …/{doc_id}, so they're already safe.
            if resolved is not None and resolved.name == doc_id:
                product_dirs.append(resolved)
        product_dirs.extend(_doc_storage_dir(docs_dir, doc_id, ct) for ct in CONTENT_TYPE_DIRS)
        product_dirs.append(docs_dir / doc_id)  # legacy flat layout
        # The indexed path is resolved and the layout candidates are not, so the
        # same directory can appear twice under two spellings.
        product_dirs = list(dict.fromkeys(p.resolve() for p in product_dirs))

        set_aside: list[tuple[Path, Path]] = []
        leftovers: list[Path] = []  # tombstones an earlier delete did not clear
        try:
            for candidate in product_dirs:
                tombstone = candidate.with_name(candidate.name + _DELETING_SUFFIX)
                if candidate.exists():
                    if tombstone.exists():
                        # A live directory wins over a stale tombstone of itself.
                        shutil.rmtree(tombstone)
                    os.replace(candidate, tombstone)
                    set_aside.append((candidate, tombstone))
                elif tombstone.exists():
                    leftovers.append(tombstone)
        except BaseException:
            _put_back_set_aside(set_aside)
            raise

        # SQLite is the commit point; JSON is rewritten from it. There is no
        # JSON-only fallback: a delete recorded in JSON alone is undone by the
        # next export, which rebuilds JSON from a database that still holds the
        # row — the caller would have been told the document was gone and then
        # found it back.
        from mantisfetch_common import doc_index_store as dis

        try:
            had_index_entry = entry is not None
            # entry was looked up pre-delete; if missing from SQLite, check list.
            # Both of those go through list_documents, which is what migrates a
            # legacy JSON-only library into the database. Keep them before the
            # delete: dis.delete_document does not migrate, so on a library that
            # has never been migrated it would remove nothing and the export
            # below — which does migrate — would import the row straight back.
            if not had_index_entry:
                had_index_entry = any(
                    d.get("id") == doc_id for d in dis.list_documents(docs_dir)
                )
            dis.delete_document(docs_dir, doc_id)
        except BaseException:
            _put_back_set_aside(set_aside)
            raise
        _export_index_json(docs_dir)

        leftovers.extend(tombstone for _, tombstone in set_aside)
        for tombstone in leftovers:
            try:
                shutil.rmtree(tombstone)
            except OSError as exc:
                logger.warning(
                    "Deleted %s, but could not clear %s; it is removed at the next start: %s",
                    doc_id,
                    tombstone,
                    exc,
                )
        return bool(set_aside) or had_index_entry


def _finish_interrupted_deletes(docs_dir: Path) -> tuple[int, int]:
    """Settle the deletes a previous process did not finish; (restored, cleared).

    A ``{doc_id}.deleting`` directory is a delete that renamed its products
    aside and then stopped. Whether it happened is whatever the index says:
    a document still indexed and with nothing at the original path is put back,
    because its delete never committed; anything else is cleared, because either
    the delete committed or a live copy has taken the place since.

    Refuses to guess. If the database cannot be read, nothing is touched — an
    empty answer here would clear the products of every document whose delete
    had not committed.
    """
    from mantisfetch_common import doc_index_store as dis

    restored = cleared = 0
    with _doc_index_lock:
        try:
            entries = dis.list_documents(docs_dir)
        except Exception as exc:  # noqa: BLE001 - the sweep waits for a readable index
            logger.warning("Interrupted-delete sweep skipped, index unreadable: %s", exc)
            return 0, 0
        indexed = {e["id"] for e in entries if isinstance(e.get("id"), str)}
        parents = {docs_dir, *(docs_dir / ct for ct in CONTENT_TYPE_DIRS)}
        for entry in entries:
            resolved = _resolve_index_storage_path(docs_dir, entry.get("storage_path"))
            if resolved is not None:
                parents.add(resolved.parent)
        for parent in parents:
            if not parent.is_dir():
                continue
            for tombstone in parent.glob(f"*{_DELETING_SUFFIX}"):
                if not tombstone.is_dir():
                    continue
                doc_id = tombstone.name[: -len(_DELETING_SUFFIX)]
                original = tombstone.with_name(doc_id)
                try:
                    if doc_id in indexed and not original.exists():
                        os.replace(tombstone, original)
                        restored += 1
                    else:
                        shutil.rmtree(tombstone)
                        cleared += 1
                except OSError as exc:
                    logger.warning("Could not settle %s: %s", tombstone, exc)
    return restored, cleared


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
    """One entry, by primary key — not by loading and scanning the whole index."""
    try:
        from mantisfetch_common import doc_index_store as dis

        return dis.get_document(docs_dir, doc_id)
    except Exception:
        # Same fallback as _load_doc_index: a database that cannot be opened at
        # all is read from the JSON export instead.
        for entry in _load_doc_index(docs_dir):
            if entry.get("id") == doc_id:
                return entry
        return None


def _resolve_doc_dir(
    docs_dir: Path, doc_id: str, entry: dict[str, Any] | None = None
) -> Path:
    """The document's directory on disk.

    ``entry`` is the caller's already-loaded index row, passed in to avoid a
    lookup — a library-wide scan has one per document, and reloading the index
    inside each of them is what made searching a 1,000-document library take
    2.7 seconds. It is a *hint*: everything below still verifies the manifest is
    actually there and still falls through to the layout scan when it is not,
    so a stale row costs a scan rather than a wrong answer.
    """
    _validate_doc_id(doc_id)
    if entry is None:
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
        "kind": manifest.get("kind") or "parsed",
        "summary_mode": summary_meta.get("mode"),
        "summary_status": summary_meta.get("status"),
        "summary_error_code": summary_meta.get("error_code"),
    }

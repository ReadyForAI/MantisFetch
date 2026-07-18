"""SQLite-backed document index + FTS5 for library scale (B3).

Keeps ``doc-index.json`` as a compatibility export (rewritten on every mutate)
while serving list/load/search from a single-file SQLite DB under the docs root:

    {docs_dir}/.doc-index.sqlite

FTS5 table ``docs_fts`` is updated when search text caches are written so
``/library/search_text`` can shortlist candidates without scanning every doc.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from mantisfetch_common.atomic import _write_json

_DB_NAME = ".doc-index.sqlite"
_local = threading.local()


def _db_path(docs_dir: Path) -> Path:
    return docs_dir / _DB_NAME


def _connect(docs_dir: Path) -> sqlite3.Connection:
    """Process-thread local connection (sqlite3 is not free-threaded by default)."""
    key = str(docs_dir.resolve())
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", {})
    conn = cache.get(key)
    if conn is not None:
        return conn
    path = _db_path(docs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            entry_json TEXT NOT NULL,
            content_type TEXT,
            source TEXT,
            created_at TEXT,
            content_hash TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            doc_id UNINDEXED,
            body,
            tokenize = 'unicode61'
        )
        """
    )
    conn.commit()
    cache[key] = conn
    _local.conns = cache
    return conn


def ensure_migrated_from_json(docs_dir: Path) -> None:
    """One-shot import of doc-index.json into SQLite.

    Uses a meta flag so an intentionally empty library after deletes is not
    re-filled from a stale JSON export.
    """
    conn = _connect(docs_dir)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    flag = conn.execute(
        "SELECT value FROM meta WHERE key = 'json_migrated'"
    ).fetchone()
    if flag is not None:
        return
    index_path = docs_dir / "doc-index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = None
        docs = index.get("documents") if isinstance(index, dict) else None
        if isinstance(docs, list):
            for entry in docs:
                if isinstance(entry, dict) and entry.get("id"):
                    _upsert_entry_conn(conn, entry)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('json_migrated', '1')"
    )
    conn.commit()


def _upsert_entry_conn(conn: sqlite3.Connection, entry: dict[str, Any]) -> None:
    doc_id = str(entry["id"])
    conn.execute(
        """
        INSERT INTO documents (id, entry_json, content_type, source, created_at, content_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            entry_json=excluded.entry_json,
            content_type=excluded.content_type,
            source=excluded.source,
            created_at=excluded.created_at,
            content_hash=excluded.content_hash
        """,
        (
            doc_id,
            json.dumps(entry, ensure_ascii=False),
            entry.get("content_type"),
            entry.get("source"),
            entry.get("created_at"),
            entry.get("content_hash"),
        ),
    )


def upsert_document(docs_dir: Path, entry: dict[str, Any]) -> None:
    """Insert/update one document entry in SQLite (caller holds _doc_index_lock)."""
    ensure_migrated_from_json(docs_dir)
    conn = _connect(docs_dir)
    _upsert_entry_conn(conn, entry)
    conn.commit()


def delete_document(docs_dir: Path, doc_id: str) -> None:
    conn = _connect(docs_dir)
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.execute("DELETE FROM docs_fts WHERE doc_id = ?", (doc_id,))
    conn.commit()


def list_documents(docs_dir: Path) -> list[dict[str, Any]]:
    ensure_migrated_from_json(docs_dir)
    conn = _connect(docs_dir)
    rows = conn.execute("SELECT entry_json FROM documents").fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            entry = json.loads(row["entry_json"])
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def export_json(docs_dir: Path, *, last_updated: str | None = None) -> None:
    """Rewrite doc-index.json from SQLite (compatibility export)."""
    docs = list_documents(docs_dir)
    index = {
        "version": 2,
        "documents": docs,
        "last_updated": last_updated or "",
    }
    _write_json(docs_dir / "doc-index.json", index)


def upsert_fts(docs_dir: Path, doc_id: str, body: str) -> None:
    """Replace FTS body for a document (caller holds lock or is single-writer)."""
    conn = _connect(docs_dir)
    conn.execute("DELETE FROM docs_fts WHERE doc_id = ?", (doc_id,))
    if body.strip():
        conn.execute(
            "INSERT INTO docs_fts (doc_id, body) VALUES (?, ?)",
            (doc_id, body),
        )
    conn.commit()


def search_fts(docs_dir: Path, query: str, *, limit: int = 50) -> list[str]:
    """Return doc_ids matching the FTS query (empty if FTS unavailable / no hits)."""
    ensure_migrated_from_json(docs_dir)
    # Escape FTS5 special chars: use phrase query for the whole string when possible.
    q = query.strip()
    if not q:
        return []
    # Build a safe MATCH: quote the query as a phrase.
    phrase = '"' + q.replace('"', '""') + '"'
    conn = _connect(docs_dir)
    try:
        rows = conn.execute(
            "SELECT doc_id FROM docs_fts WHERE docs_fts MATCH ? LIMIT ?",
            (phrase, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(r["doc_id"]) for r in rows]

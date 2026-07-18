"""Browser session object + manager (TTL/LRU eviction with cleanup callbacks).

`Session` holds a Playwright context/page plus per-session distill cache and a
concurrency lock; `SessionManager` is the TTL+maxsize store that closes the
context on eviction/expiry (fixing the resource leak a plain TTLCache had). The
process-wide `sessions` singleton is shared by all endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger("mantisfetch_browser")


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using default %d", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%r is < %d; using default %d", name, raw, minimum, default)
        return default
    return value


# Defaults match historical hardcodes; override via env for long-lived agents /
# high concurrency without a rebuild.
SESSION_TTL_SECONDS = _env_int("MANTISFETCH_SESSION_TTL_SEC", 30 * 60, minimum=1)
SESSION_MAXSIZE = _env_int("MANTISFETCH_SESSION_MAXSIZE", 200, minimum=1)


@dataclass
class Session:
    context: BrowserContext
    page: Page
    lang: str
    last_distill: dict[str, Any] | None = None
    action_map: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # ✅ IMPROVED: field(default_factory)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # concurrency lock
    closed: bool = False  # set when session is evicted/expired
    # WebMCP: cached tool list
    webmcp_tools: list[dict[str, Any]] | None = None
    webmcp_available: bool = False


# ============================================================
# SessionManager with expiry callbacks,
#    replaces TTLCache to fix resource leak on expired sessions
# ============================================================
class SessionManager:
    def __init__(self, ttl: int = SESSION_TTL_SECONDS, maxsize: int = SESSION_MAXSIZE):
        self._sessions: OrderedDict[str, tuple[float, Session]] = OrderedDict()
        self._ttl = ttl
        self._maxsize = maxsize
        self._lock = asyncio.Lock()

    def __len__(self):
        return len(self._sessions)

    # NOTE: _close_session() awaits context.close() (browser I/O) and must run
    # OUTSIDE self._lock — holding the manager lock across it would serialize
    # every other session operation behind a slow close.

    async def put(self, sid: str, sess: Session) -> None:
        evicted: Session | None = None
        async with self._lock:
            # evict oldest
            if len(self._sessions) >= self._maxsize:
                old_sid, (_, evicted) = self._sessions.popitem(last=False)
                logger.info("session evicted (maxsize): %s", old_sid)
            self._sessions[sid] = (time.time(), sess)
        if evicted is not None:
            await self._close_session(evicted)

    async def get(self, sid: str) -> Session | None:
        async with self._lock:
            item = self._sessions.get(sid)
            if not item:
                return None
            ts, sess = item
            if time.time() - ts > self._ttl:
                del self._sessions[sid]
            else:
                # refresh timestamp & move to end
                self._sessions[sid] = (time.time(), sess)
                self._sessions.move_to_end(sid)
                return sess
        # only reached when the session had expired
        logger.info("session expired on access: %s", sid)
        await self._close_session(sess)
        return None

    async def remove(self, sid: str) -> None:
        async with self._lock:
            item = self._sessions.pop(sid, None)
        if item:
            _, sess = item
            await self._close_session(sess)

    async def cleanup(self) -> None:
        """Periodic cleanup of expired sessions."""
        async with self._lock:
            now = time.time()
            expired_ids = [sid for sid, (ts, _) in self._sessions.items() if now - ts > self._ttl]
            expired = [(sid, self._sessions.pop(sid)[1]) for sid in expired_ids]
        for sid, sess in expired:
            logger.info("session expired (cleanup): %s", sid)
            await self._close_session(sess)

    async def close_all(self) -> None:
        async with self._lock:
            items = list(self._sessions.values())
            self._sessions.clear()
        for _, sess in items:
            await self._close_session(sess)

    @staticmethod
    async def _close_session(sess: Session):
        # Mark closed first so new operations reject, then wait for any in-flight
        # goto/distill/act (which holds sess.lock) to finish before closing the
        # context — otherwise it hits a random Playwright TargetClosed mid-op.
        # Safe from deadlock: _close_session always runs outside the manager lock,
        # and endpoints acquire the manager lock (via get) before sess.lock, never
        # the reverse.
        sess.closed = True
        async with sess.lock:
            try:
                await sess.context.close()
            except Exception:
                pass


sessions = SessionManager()

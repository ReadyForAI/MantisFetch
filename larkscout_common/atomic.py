"""Atomic file writes shared across services (unique temp file + fsync + replace).

A partially written index or section file can corrupt the shared document
library; every persisted artifact goes through one of these helpers so a crash
mid-write leaves the previous file intact. Each write goes to a *unique* temp
file in the destination directory (so two concurrent writers to the same path
can't clobber each other's temp), is fsync'd for durability, then atomically
renamed over the target via os.replace.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes to a unique temp file in path's dir, fsync, then os.replace."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # Leave mkstemp's owner-only 0600 perms: the doc library is private and
        # served via the API, so we never widen access (a chmod here would make
        # artifacts world-readable on a restrictive-umask deployment).
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_text(path: Path, content: str) -> None:
    """Write text atomically (unique temp + fsync + replace)."""
    _atomic_write(path, content.encode("utf-8"))


def _write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (accepts dict or list; unique temp + fsync + replace)."""
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def _write_bytes(path: Path, content: bytes) -> None:
    """Write bytes atomically (unique temp + fsync + replace)."""
    _atomic_write(path, content)

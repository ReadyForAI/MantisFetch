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
        # mkstemp forces owner-only 0600, which leaves the artifact unreadable to
        # a group member or SMB account even when the library directory is shared.
        # Keep owner rw and inherit only the destination directory's *group* rw
        # bits — never widen to other. A group-shared dir (0775) yields group-rw
        # 0660; a private dir (0700) stays owner-only 0600. No world exposure.
        os.chmod(tmp_name, 0o600 | (os.stat(path.parent).st_mode & 0o060))
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

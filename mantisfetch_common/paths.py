"""Path helpers shared across services."""

import os
from pathlib import Path


def _mask_path(p: str | Path) -> str:
    """Replace home directory prefix with ~ to avoid exposing absolute paths."""
    s = str(p)
    home = os.path.expanduser("~")
    return s.replace(home, "~") if s.startswith(home) else s


def open_within(root: Path, target: Path) -> int:
    """Open ``target`` for reading by descending from ``root`` one component at a
    time, each with ``O_NOFOLLOW``, and return the file descriptor.

    ``target`` must be ``root`` or a canonical path under it — resolved and
    checked for containment by the caller. Walking component-by-component with
    no-follow means no path element below the root can be a symlink, closing
    the race where a component the caller checked is swapped for a symlink to
    outside the fence before the open, which plain ``os.open(target)`` (or
    leaf-only ``O_NOFOLLOW``) would follow. Raises ``OSError`` if any component
    is a symlink, is missing, or is not the expected file/directory.

    Shared by the deliverables byte endpoint and MCP's ``rel_path`` source.
    """
    rel_parts = target.relative_to(root).parts
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in rel_parts[:-1]:
            nxt = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = nxt
        # O_NONBLOCK so a FIFO swapped in (or left) at the leaf returns at once
        # instead of waiting for a writer; it changes nothing for a regular
        # file, and callers reject anything else on the descriptor.
        return os.open(
            rel_parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd
        )
    finally:
        os.close(dir_fd)

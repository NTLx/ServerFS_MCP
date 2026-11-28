"""FD-based filesystem traversal primitives (Linux product path).

The platform-shared POSIX core lives in ``posix_fdio.py`` (verified identical
on Linux and Darwin); this module re-exports it for the Linux call path and
adds the Linux-specific ``/proc/self/fd`` re-resolution used to give child
processes (``rg``) a cwd anchored to an already-validated directory FD.
"""

from __future__ import annotations

from .posix_fdio import (  # noqa: F401  (re-export for the Linux call path)
    _DIR_FLAGS,
    _FILE_FLAGS,
    _TEMP_FLAGS,
    _map_open_error,
    create_temp_at,
    fsync_directory,
    open_dir_at,
    open_directory_fd,
    open_file_fd,
    open_regular_at,
    open_root,
    root_fd,
    stat_at,
    stat_final,
    unlink_at,
    walk_parent_dirs,
)


def proc_fd_path(fd: int) -> str:
    """/proc/self/fd/N — used as a child-process cwd so rg operates on the
    exact directory object we validated (pass_fds keeps it alive).

    Linux-only: there is no /proc on macOS; the Darwin backend must never
    re-resolve an FD through a path.
    """
    return f"/proc/self/fd/{fd}"

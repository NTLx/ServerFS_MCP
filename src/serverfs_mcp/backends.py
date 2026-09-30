"""Platform-neutral filesystem backend contract (v0.10 Phase A).

ServerFS splits the MCP product layer (tools, policy, limits, audit) from the
platform filesystem kernel. Each platform implements one backend; the MCP
surface talks only to this contract:

    FilesystemBackend / session contract
        Linux backend  (fdio.py: openat/dir_fd/O_NOFOLLOW)
        Windows native backend (later phase: HANDLE-relative traversal)

Phase A keeps Linux on its proven implementation. The contract below
deliberately mirrors what the Linux call sites already do, so moving them
behind the seam is a mechanical, reviewable change:

- revision ownership moves behind the backend (``revision_of``); Python must
  not assume ``os.stat_result`` or POSIX inode semantics on Windows;
- roots are opened once per operation on Linux (same FD discipline as v0.9);
  a native backend may retain a root capability for the process lifetime
  (§10.1) — the session object is where that difference lives;
- every channel (read/list/find/search/stat/mutation) goes through the same
  backend, so "all channels filter identically" (paths.py) keeps holding per
  platform rather than per module.

No public MCP tool signature changes in Phase A.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

from .fdio import open_directory_fd, open_file_fd, root_fd
from .fdio import stat_final as fdio_stat_final

if TYPE_CHECKING:
    from .paths import ResolvedPath


class BackendError(Exception):
    """Backend-raised failure that the tool layer maps to a coded ToolError.

    The code/message pair is the agent-facing contract; backends must never
    leak host paths, raw NTSTATUS/errno strings or object identifiers here.
    """


class FilesystemBackend(Protocol):
    """The operations every platform backend must provide.

    The Linux implementation is a thin composition of the existing fdio
    primitives; nothing in this protocol re-derives policy or re-validates
    paths — callers pass a policy-validated ``ResolvedPath`` exactly as the
    v0.9 call sites do.
    """

    def root_fd(self, resolved: ResolvedPath):
        """Context manager yielding the trusted root anchor for one operation."""
        ...

    def revision_of(self, st) -> str:
        """Opaque revision token for one stat-like object of this backend."""
        ...

    def open_directory(self, root_fd, rel_parts: tuple[str, ...]):
        """Context manager: open rel_parts as a directory under the root."""
        ...

    def open_regular_file(self, root_fd, rel_parts: tuple[str, ...]):
        """Context manager: open the final component as a regular file."""
        ...

    def stat_final(self, root_fd, rel_parts: tuple[str, ...]):
        """Stat the final component through the backend's traversal."""
        ...


class LinuxBackend:
    """The v0.9 Linux filesystem kernel behind the Phase A seam.

    Every method delegates to ``fdio`` exactly as the previous direct call
    sites did — this class is a seam, not a redesign (§4.8). It exists so
    call sites depend on the contract rather than on Linux module internals,
    which is what later allows the Windows backend to slot in.
    """

    def root_fd(self, resolved: ResolvedPath):
        return root_fd(str(resolved.workdir.container_path))

    def revision_of(self, st: os.stat_result) -> str:
        # Imported here (not at module top) to break the backends ↔
        # mutations cycle: mutations imports get_backend for its root seam,
        # so backends can only pull compute_revision lazily.
        from .mutations import compute_revision

        return compute_revision(st)

    def open_directory(self, root_fd: int, rel_parts: tuple[str, ...]):
        return open_directory_fd(root_fd, rel_parts)

    def open_regular_file(self, root_fd: int, rel_parts: tuple[str, ...]):
        return open_file_fd(root_fd, rel_parts)

    def stat_final(self, root_fd: int, rel_parts: tuple[str, ...]):
        return fdio_stat_final(root_fd, rel_parts)


def get_backend() -> LinuxBackend:
    """Return the platform backend for this process (v0.10 Phase A: Linux).

    Phase B introduces platform dispatch; until then every caller gets the
    Linux kernel so the seam is exercised without changing behavior.
    """
    return LinuxBackend()


__all__ = [
    "BackendError",
    "FilesystemBackend",
    "LinuxBackend",
    "get_backend",
]

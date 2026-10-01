"""Platform-neutral filesystem backend contract (v0.10 Phase A closure).

ServerFS splits the MCP product layer (tools, policy, limits, audit) from
the platform filesystem kernel. The boundary is deliberately HIGH-LEVEL
(§11): the product layer never touches platform primitives — no FDs on
Linux, no HANDLEs on Windows. Every filesystem channel speaks to a
``WorkdirSession`` returned by ``open_session``:

    open_session(workdir) -> WorkdirSession   # capability-scoped
        stat / list / read_text_page / read_binary / find / search
        create_file / replace_file / delete_file
        create_binary_file / replace_binary_file
        create_directory / delete_directory

The Linux implementation lives in ``linux_backend.py`` and wraps the proven
v0.9 fdio/mutations/search code behind this shape; the future Windows
implementation maps the same methods onto a Rust/PyO3 native module whose
handles never cross the FFI boundary into general Python code.

This module is the import-safety boundary (§11): it must stay loadable on a
platform where ``fdio`` or Unix-only stdlib modules cannot be imported, so
nothing below ``import``-level here may pull a platform kernel. ``get_backend``
lazy-imports the kernel chosen for this process; platform dispatch arrives in
Phase B.

Rules frozen here (§11):

- platform objects (FD/HANDLE/stat-like values) never leak to the product
  layer; sessions return plain data (models, bytes, strings, opaque
  revision tokens);
- revision ownership is backend-side: the product layer only compares
  opaque tokens it previously received;
- error normalization is backend-side: ``BackendError`` carries the
  agent-facing CODE, not a raw errno/NTSTATUS;
- no session method accepts an absolute host path after the session has
  been created — only already policy-validated ``ResolvedPath`` objects.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .binary_payload import BinaryRead
    from .models import (
        CreateDirectoryResult,
        CreateTextFileResult,
        DeleteDirectoryResult,
        DeleteFileResult,
        EditTextFileResult,
        EntryInfo,
        StatFileResult,
        TextEdit,
        TextMatch,
        UploadBinaryFileResult,
    )
    from .paths import ResolvedPath
    from .workdirs import Workdir


class BackendError(Exception):
    """Backend-raised failure that the tool layer maps to a coded ToolError.

    ``code``/``message`` are the agent-facing contract; backends must never
    leak host paths, raw NTSTATUS/errno strings or object identifiers here.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TextPage:
    """Plain-data result of one paginated text read (no FDs, no stat)."""

    __slots__ = (
        "revision",
        "lines",
        "bytes_returned",
        "end_line",
        "has_more",
        "has_nul",
        "has_bom",
    )

    def __init__(
        self,
        *,
        revision: str,
        lines: list[bytes],
        bytes_returned: int,
        end_line: int,
        has_more: bool,
        has_nul: bool,
        has_bom: bool,
    ):
        self.revision = revision
        self.lines = lines
        self.bytes_returned = bytes_returned
        self.end_line = end_line
        self.has_more = has_more
        self.has_nul = has_nul
        self.has_bom = has_bom


class WorkdirSession(Protocol):
    """The capability-scoped operations one workdir exposes.

    The Linux implementation opens the root per operation exactly as v0.9
    did; a native backend may retain the root capability for the process
    lifetime (§10.1) — that difference stays invisible behind this shape.
    Every method is part of the contract a new platform kernel must
    implement; ``tests/test_backends.py`` enforces protocol coverage.
    """

    # ---- read channels ----

    def stat(self, resolved: ResolvedPath) -> StatFileResult: ...
    def list(
        self, resolved: ResolvedPath, *, offset: int, limit: int
    ) -> tuple[list[EntryInfo], bool]: ...
    def find(
        self, resolved: ResolvedPath, *, pattern: str, limit: int, max_walk_entries: int
    ) -> tuple[list[str], bool]: ...
    def search(
        self,
        resolved: ResolvedPath,
        *,
        query: str,
        glob: str | None,
        case_sensitive: bool,
        limit: int,
        timeout_seconds: float,
        max_file_bytes: int,
    ) -> tuple[list[TextMatch], bool]: ...
    def read_text_page(
        self,
        resolved: ResolvedPath,
        *,
        start_line: int,
        max_lines: int,
        max_read_bytes: int,
        binary_sample: int,
    ) -> TextPage: ...
    def read_binary(self, resolved: ResolvedPath, *, max_bytes: int) -> BinaryRead: ...

    def validate_directory(self, resolved: ResolvedPath) -> None:
        """Open-and-close one directory to surface PATH/NOT_DIRECTORY errors
        before the channel work starts (v0.9 find/search pre-open)."""
        ...

    # ---- mutation channels ----

    def create_file(
        self, resolved: ResolvedPath, content: str, *, max_write_bytes: int
    ) -> CreateTextFileResult: ...
    def replace_file(
        self,
        resolved: ResolvedPath,
        expected_revision: str,
        edits: list[TextEdit],
        *,
        max_write_bytes: int,
        max_edits_per_call: int,
    ) -> EditTextFileResult: ...
    def delete_file(self, resolved: ResolvedPath, expected_revision: str) -> DeleteFileResult: ...
    def create_binary_file(
        self, resolved: ResolvedPath, data: bytes, *, max_binary_bytes: int
    ) -> UploadBinaryFileResult: ...
    def replace_binary_file(
        self,
        resolved: ResolvedPath,
        data: bytes,
        expected_revision: str,
        *,
        max_binary_bytes: int,
    ) -> UploadBinaryFileResult: ...
    def create_directory(self, resolved: ResolvedPath) -> CreateDirectoryResult: ...
    def delete_directory(
        self, resolved: ResolvedPath, expected_revision: str
    ) -> DeleteDirectoryResult: ...


class FilesystemBackend(Protocol):
    """Per-platform filesystem kernel factory.

    ``open_session`` is the only entry the product layer uses. It performs
    no policy validation — the caller passes an already-authorized workdir
    and receives a session bound to that workdir's trusted root.
    """

    def open_session(self, workdir: Workdir) -> WorkdirSession: ...


def get_backend() -> FilesystemBackend:
    """Return the platform backend for this process (Phase B dispatch).

    Each kernel module is imported lazily so this contract module loads on
    every platform without touching the other platform's kernel: a Windows
    process never imports fdio, and a Linux process never imports the
    native extension. The Windows backend is a process singleton because
    its sessions retain root capabilities for the process lifetime (§10.1);
    the Linux backend is stateless and re-opened cheaply per call, matching
    v0.9 behavior exactly.
    """
    if sys.platform == "win32":
        from .windows_backend import WindowsBackend

        return WindowsBackend.shared()
    from .linux_backend import LinuxBackend

    return LinuxBackend()


__all__ = [
    "BackendError",
    "FilesystemBackend",
    "TextPage",
    "WorkdirSession",
    "get_backend",
]

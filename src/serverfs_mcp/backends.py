"""Platform-neutral filesystem backend contract (v0.10 Phase A closure).

ServerFS splits the MCP product layer (tools, policy, limits, audit) from
the platform filesystem kernel. The boundary is deliberately HIGH-LEVEL
(§11): the product layer never touches platform primitives — no FDs on
Linux, no HANDLEs on Windows. Every filesystem channel speaks to a
``WorkdirSession`` returned by ``open_session``:

    open_session(workdir) -> WorkdirSession   # capability-scoped
        stat / list / read / read_binary / find / search
        create_file / replace_file / delete_file
        create_directory / delete_directory
        read_text_page (paginated text read with revision stability)

The Linux implementation wraps the proven v0.9 fdio/mutations/search code
behind that shape; the future Windows implementation maps the same methods
onto a Rust/PyO3 native module whose handles never cross the FFI boundary
into general Python code.

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

Phase A closure keeps Linux behavior bit-for-bit: every method below
delegates to the exact v0.9 code path, so the MCP surface observes no
change. Platform dispatch (Windows) arrives in Phase B.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

from .fdio import open_directory_fd, open_file_fd, root_fd
from .models import (
    CreateDirectoryResult,
    CreateTextFileResult,
    DeleteDirectoryResult,
    DeleteFileResult,
    StatFileResult,
    TextMatch,
)

if TYPE_CHECKING:
    from .models import TextEdit
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


class WorkdirSession(Protocol):
    """The capability-scoped operations one workdir exposes.

    The Linux implementation opens the root per operation exactly as v0.9
    did; a native backend may retain the root capability for the process
    lifetime (§10.1) — that difference stays invisible behind this shape.
    """

    # ---- read channels ----

    def stat(self, resolved: ResolvedPath) -> StatFileResult: ...
    def list(self, resolved: ResolvedPath, *, offset: int, limit: int): ...
    def find(self, resolved: ResolvedPath, *, pattern: str, limit: int, max_walk_entries: int): ...
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
    ): ...
    def read_binary(self, resolved: ResolvedPath, *, max_bytes: int): ...

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
    ): ...
    def delete_file(self, resolved: ResolvedPath, expected_revision: str) -> DeleteFileResult: ...
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


class LinuxWorkdirSession:
    """High-level session backed by the v0.9 Linux fdio kernel.

    Every method is a mechanical delegation to the exact v0.9 call path
    (filesystem.py / mutations.py / binary.py / search.py), so released
    Linux behavior is unchanged. This class is where the fdio dependency
    lives; the product layer (tools.py) holds none.
    """

    def __init__(self, workdir: Workdir):
        self._workdir = workdir

    # ---- root anchor (per operation, exactly as v0.9) ----

    def _root(self, resolved: ResolvedPath):
        return root_fd(str(resolved.workdir.root))

    # ---- read channels ----

    def stat(self, resolved: ResolvedPath) -> StatFileResult:
        from .filesystem import stat_file

        return stat_file(resolved)

    def list(self, resolved: ResolvedPath, *, offset: int, limit: int):
        from .filesystem import list_directory

        return list_directory(resolved, offset=offset, limit=limit)

    def find(self, resolved: ResolvedPath, *, pattern: str, limit: int, max_walk_entries: int):
        from .filesystem import find_files

        return find_files(resolved, pattern=pattern, limit=limit, max_walk_entries=max_walk_entries)

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
    ) -> tuple[list[TextMatch], bool]:
        from .search import run_search

        with root_fd(str(resolved.workdir.root)) as root:
            with open_directory_fd(root, resolved.rel_parts) as search_root:
                return run_search(
                    search_root,
                    resolved,
                    query=query,
                    glob=glob,
                    case_sensitive=case_sensitive,
                    limit=limit,
                    timeout_seconds=timeout_seconds,
                    max_file_bytes=max_file_bytes,
                )

    def read_text_page(
        self,
        resolved: ResolvedPath,
        *,
        start_line: int,
        max_lines: int,
        max_read_bytes: int,
        binary_sample: int,
    ):
        """Paginated UTF-8 text read with before/after revision stability.

        This is the v0.9 tools._read_text_file_impl file-reading core lifted
        behind the session seam. The product layer keeps only the policy
        resolution, limit math and ToolError mapping.

        compute_revision is resolved at CALL time (attribute lookup on the
        mutations module) so tests can monkeypatch it as the revision
        authority of this backend.
        """
        with self._root(resolved) as root:
            with open_file_fd(root, resolved.rel_parts) as fd:
                return self._read_page_from_fd(
                    fd,
                    start_line=start_line,
                    max_lines=max_lines,
                    max_read_bytes=max_read_bytes,
                    binary_sample=binary_sample,
                )

    @staticmethod
    def _read_page_from_fd(
        fd: int,
        *,
        start_line: int,
        max_lines: int,
        max_read_bytes: int,
        binary_sample: int,
    ) -> _TextPage:
        from . import mutations

        revision_of = mutations.compute_revision
        revision = revision_of(os.fstat(fd))
        with open(fd, "rb", closefd=False) as fh:
            sample = fh.read(binary_sample)
            has_nul = b"\x00" in sample
            bom = sample.startswith(b"\xef\xbb\xbf")
            fh.seek(0)
            lines: list[bytes] = []
            line_no = 0
            bytes_returned = 0
            end_line = start_line - 1
            has_more = False
            for raw in fh:
                line_no += 1
                if line_no == 1 and bom:
                    raw = raw[3:]
                if line_no < start_line:
                    continue
                if len(raw) > max_read_bytes:
                    raise BackendError(
                        "LINE_TOO_LARGE", f"line {line_no} exceeds {max_read_bytes} bytes"
                    )
                if len(lines) >= max_lines:
                    has_more = True
                    break
                if bytes_returned + len(raw) > max_read_bytes:
                    has_more = True
                    break
                lines.append(raw)
                bytes_returned += len(raw)
                end_line = line_no
        if revision_of(os.fstat(fd)) != revision:
            raise BackendError("FILE_CHANGED_DURING_READ", "file changed while being read")

        return _TextPage(
            revision=revision,
            lines=lines,
            bytes_returned=bytes_returned,
            end_line=end_line,
            has_more=has_more,
            has_nul=has_nul,
            has_bom=bom,
        )

    def read_binary(self, resolved: ResolvedPath, *, max_bytes: int):
        from .binary import read_binary_file

        return read_binary_file(resolved, max_bytes=max_bytes)

    def validate_directory(self, resolved: ResolvedPath) -> None:
        # v0.9 opened the search/find root once before delegating so
        # PATH_NOT_FOUND / NOT_A_DIRECTORY surface from this call, not from
        # deep inside the walk. Same contract, now behind the seam.
        with root_fd(str(resolved.workdir.root)) as root:
            with open_directory_fd(root, resolved.rel_parts):
                pass

    # ---- mutation channels ----

    def create_binary_file(self, resolved: ResolvedPath, data: bytes, *, max_binary_bytes: int):
        from .mutations import create_binary_file

        return create_binary_file(resolved, data, max_binary_bytes=max_binary_bytes)

    def replace_binary_file(
        self,
        resolved: ResolvedPath,
        data: bytes,
        expected_revision: str,
        *,
        max_binary_bytes: int,
    ):
        from .mutations import replace_binary_file

        return replace_binary_file(
            resolved, data, expected_revision, max_binary_bytes=max_binary_bytes
        )

    def create_file(
        self, resolved: ResolvedPath, content: str, *, max_write_bytes: int
    ) -> CreateTextFileResult:
        from .mutations import create_text_file

        return create_text_file(resolved, content, max_write_bytes=max_write_bytes)

    def replace_file(
        self,
        resolved: ResolvedPath,
        expected_revision: str,
        edits: list[TextEdit],
        *,
        max_write_bytes: int,
        max_edits_per_call: int,
    ):
        from .mutations import edit_text_file

        return edit_text_file(
            resolved,
            expected_revision,
            edits,
            max_write_bytes=max_write_bytes,
            max_edits_per_call=max_edits_per_call,
        )

    def delete_file(self, resolved: ResolvedPath, expected_revision: str) -> DeleteFileResult:
        from .mutations import delete_file

        return delete_file(resolved, expected_revision)

    def create_directory(self, resolved: ResolvedPath) -> CreateDirectoryResult:
        from .mutations import create_directory

        return create_directory(resolved)

    def delete_directory(
        self, resolved: ResolvedPath, expected_revision: str
    ) -> DeleteDirectoryResult:
        from .mutations import delete_directory

        return delete_directory(resolved, expected_revision)


class _TextPage:
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


class LinuxBackend:
    """The v0.9 Linux filesystem kernel behind the session contract."""

    def open_session(self, workdir: Workdir) -> LinuxWorkdirSession:
        return LinuxWorkdirSession(workdir)


def get_backend() -> LinuxBackend:
    """Return the platform backend for this process (Phase A: Linux).

    Phase B introduces platform dispatch; until then every caller gets the
    Linux kernel so the seam is exercised without changing behavior.
    """
    return LinuxBackend()


__all__ = [
    "BackendError",
    "FilesystemBackend",
    "LinuxBackend",
    "LinuxWorkdirSession",
    "WorkdirSession",
    "get_backend",
]

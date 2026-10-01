"""The Linux filesystem kernel behind the v0.10 backend session contract.

This module is the single home of the fdio dependency for the MCP product
path: ``LinuxWorkdirSession`` is a mechanical delegation to the exact v0.9
call path (filesystem.py / mutations.py / binary.py / search.py), so released
Linux behavior is unchanged. Non-Linux processes must never import this
module; ``backends.get_backend`` is the only caller and imports it lazily.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from .backends import BackendError, TextPage
from .binary import read_binary_file
from .fdio import open_directory_fd, open_file_fd, root_fd
from .filesystem import find_files, list_directory, stat_file
from .models import (
    CreateDirectoryResult,
    CreateTextFileResult,
    DeleteDirectoryResult,
    DeleteFileResult,
    StatFileResult,
    TextMatch,
)
from .search import run_search

if TYPE_CHECKING:
    from .models import TextEdit
    from .paths import ResolvedPath
    from .workdirs import Workdir


class LinuxWorkdirSession:
    """High-level session backed by the v0.9 Linux fdio kernel.

    Every method delegates to the exact v0.9 code path; this class is where
    the fdio dependency lives, and the product layer (tools.py) holds none.
    """

    def __init__(self, workdir: Workdir):
        self._workdir = workdir

    # ---- root anchor (per operation, exactly as v0.9) ----

    def _root(self, resolved: ResolvedPath):
        return root_fd(str(resolved.workdir.root))

    # ---- read channels ----

    def stat(self, resolved: ResolvedPath) -> StatFileResult:
        return stat_file(resolved)

    def list(self, resolved: ResolvedPath, *, offset: int, limit: int):
        return list_directory(resolved, offset=offset, limit=limit)

    def find(self, resolved: ResolvedPath, *, pattern: str, limit: int, max_walk_entries: int):
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
    ) -> TextPage:
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
    ) -> TextPage:
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

        return TextPage(
            revision=revision,
            lines=lines,
            bytes_returned=bytes_returned,
            end_line=end_line,
            has_more=has_more,
            has_nul=has_nul,
            has_bom=bom,
        )

    def read_binary(self, resolved: ResolvedPath, *, max_bytes: int):
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


class LinuxBackend:
    """The v0.9 Linux filesystem kernel behind the session contract."""

    def open_session(self, workdir: Workdir) -> LinuxWorkdirSession:
        return LinuxWorkdirSession(workdir)


__all__ = ["LinuxBackend", "LinuxWorkdirSession"]

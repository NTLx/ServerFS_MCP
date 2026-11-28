"""The Linux filesystem kernel behind the v0.10 backend session contract.

This module is the single home of the fdio dependency for the MCP product
path: ``LinuxWorkdirSession`` is a mechanical delegation to the exact v0.9
call path (filesystem.py / mutations.py / binary.py / search.py), so released
Linux behavior is unchanged. Non-Linux processes must never import this
module; ``backends.get_backend`` is the only caller and imports it lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .backends import TextPage
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
from .read_page import read_page_from_fd
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
        behind the session seam (and now shared verbatim with the Darwin
        backend through ``read_page.read_page_from_fd``). The product layer
        keeps only the policy resolution, limit math and ToolError mapping.
        """
        with self._root(resolved) as root:
            with open_file_fd(root, resolved.rel_parts) as fd:
                return read_page_from_fd(
                    fd,
                    start_line=start_line,
                    max_lines=max_lines,
                    max_read_bytes=max_read_bytes,
                    binary_sample=binary_sample,
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

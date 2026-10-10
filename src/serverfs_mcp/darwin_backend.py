"""Darwin filesystem backend for macOS 27 on Apple Silicon (v0.13 Phase B).

The Darwin counterpart of ``linux_backend.py``, reached only through
``backends.get_backend`` after the strict platform gate
(``darwin_platform.ensure_supported_darwin``) passes.

Architecture (probe evidence: docs/phase-0-macos27-arm64-capability-probe-
2026-10.md):

- READ channels delegate to the POSIX-shared modules ``filesystem.py`` /
  ``binary.py`` / ``read_page.py`` — descriptor-relative openat semantics
  are identical on Linux and Darwin, so they run here unchanged;
- MUTATIONS delegate to ``mutations.py`` (same reserved-temp → fsync →
  atomic publication shape). The only platform difference is metadata
  preservation on replace: Darwin uses ``fcopyfile(COPYFILE_METADATA)``
  (mode + xattrs + ACL, content untouched) instead of the Linux
  chown/chmod/xattr sequence, failing before publication on any loss;
- SEARCH never uses ``/proc/self/fd`` or rg (both Linux-only): it is an
  FD-secure directory walk → bounded regular-file read → literal UTF-8
  scan, with the shared ``search_scan``/``search_glob`` helpers so all
  three platforms decide matches identically.

No symlink is ever followed (kernel-refused via O_NOFOLLOW everywhere);
no request-derived path is ever re-resolved from a pathname.
"""

from __future__ import annotations

import contextlib
import os
import stat as stat_module
import time
from typing import TYPE_CHECKING

from . import mutations
from .backends import BackendError, TextPage
from .binary import read_binary_file
from .darwin_libc import fcopyfile_metadata
from .darwin_platform import ensure_supported_darwin
from .errors import MetadataPreservationError
from .filesystem import find_files, list_directory, stat_file
from .models import (
    CreateDirectoryResult,
    CreateTextFileResult,
    DeleteDirectoryResult,
    DeleteFileResult,
    StatFileResult,
    TextMatch,
)
from .paths import ResolvedPath, is_hidden_component, is_reserved_component
from .posix_fdio import open_directory_fd, open_file_fd, open_regular_at, root_fd
from .read_page import read_page_from_fd
from .search_glob import glob_matches
from .search_scan import ALWAYS_EXCLUDED_DIRS, rg_binary_truncate, scan_file

if TYPE_CHECKING:
    from .models import TextEdit
    from .workdirs import Workdir


def preserve_metadata_darwin(src_fd: int, dst_fd: int, original: os.stat_result) -> None:
    """Darwin replacement-metadata strategy (dev_plan_v0.13.md §9 B4).

    fcopyfile(COPYFILE_METADATA) carries mode/xattrs/ACLs; ownership is
    verified and corrected afterwards (chown clears S_ISUID/S_ISGID, so
    the mode is re-applied last). Any failure aborts BEFORE publication —
    silent metadata loss is not acceptable.
    """
    fcopyfile_metadata(src_fd, dst_fd)
    dst_st = os.fstat(dst_fd)
    if (dst_st.st_uid, dst_st.st_gid) != (original.st_uid, original.st_gid):
        try:
            os.fchown(dst_fd, original.st_uid, original.st_gid)
        except OSError as exc:
            raise MetadataPreservationError("ownership could not be preserved") from exc
    try:
        os.fchmod(dst_fd, stat_module.S_IMODE(original.st_mode))
    except OSError as exc:
        raise MetadataPreservationError("file mode could not be preserved") from exc
    if (os.fstat(dst_fd).st_uid, os.fstat(dst_fd).st_gid) != (
        original.st_uid,
        original.st_gid,
    ):
        raise MetadataPreservationError("ownership could not be preserved")


def _bounded_read(parent_fd: int, name: str, max_file_bytes: int) -> bytes | None:
    """Read one regular file bounded by max_file_bytes, FD-anchored.

    Returns None when the file is missing, not regular, or exceeds the
    size bound — the searcher skips it (rg behaves the same).
    """
    try:
        with open_regular_at(parent_fd, name) as fd:
            st = os.fstat(fd)
            if not stat_module.S_ISREG(st.st_mode) or st.st_size > max_file_bytes:
                return None
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_file_bytes:
                    return None  # grew past the bound mid-read: skip
                chunks.append(chunk)
            return b"".join(chunks)
    except OSError:
        return None  # raced/vanished/refused: rg skips too


def run_search(
    root_fd: int,
    resolved: ResolvedPath,
    *,
    query: str,
    glob: str | None,
    case_sensitive: bool,
    limit: int,
    timeout_seconds: float,
    max_file_bytes: int,
) -> tuple[list[TextMatch], bool]:
    """Literal fixed-string search over the policy-filtered FD-secure walk.

    Public behavior matches the Linux rg channel: fixed-string query,
    glob filter (search_glob semantics, relative to the SEARCH root),
    case sensitivity, max file size, wall-clock deadline, global match
    limit with true early-stop at limit+1 (truncation is proven, not
    guessed), hidden/deny policy on every candidate, VCS internals and
    reserved names never searched, NUL-containing files suppressed from
    the first NUL chunk onward.

    ``root_fd`` is the already-opened SEARCH ROOT (the caller opened
    ``resolved.rel_parts``); the walk is therefore search-root-relative
    and result paths are re-prefixed with the search root's rel_parts —
    mirroring rg's cwd-relative paths and the Linux parser's
    ``(*base_parts, *parts)`` join.
    """
    deadline = time.monotonic() + timeout_seconds
    base = resolved.rel_parts
    needle = query if case_sensitive else query.casefold()
    matches: list[TextMatch] = []
    truncated = False
    stack: list[tuple[str, ...]] = [()]
    while stack and not truncated:
        if time.monotonic() > deadline:
            raise BackendError(
                "SEARCH_TIMEOUT", f"search exceeded the {timeout_seconds}s deadline"
            )
        parts = stack.pop()
        try:
            with open_directory_fd(root_fd, parts) as current_fd:
                rows: list[tuple[str, int, int | None]] = []
                with os.scandir(current_fd) as it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue  # vanished mid-scan
                        size = st.st_size if stat_module.S_ISREG(st.st_mode) else None
                        rows.append((entry.name, st.st_mode, size))
        except (FileNotFoundError, NotADirectoryError):
            continue  # raced/vanished below the validated root
        rows.sort(key=lambda r: r[0])
        for name, mode, size in rows:
            child = (*parts, name)  # search-root-relative
            full = (*base, *child)  # workdir-relative
            if not resolved.allow_hidden and is_hidden_component(name):
                continue
            if is_reserved_component(name):
                continue  # temp artifacts / sentinel: never searched
            if resolved.deny_policy.is_denied(full):
                continue
            if stat_module.S_ISDIR(mode):
                if name in ALWAYS_EXCLUDED_DIRS:
                    continue
                stack.append(child)
                continue
            if not stat_module.S_ISREG(mode):
                continue
            if glob and not glob_matches(name, "/".join(child), glob):
                continue
            if size is None or size > max_file_bytes:
                continue
            if time.monotonic() > deadline:
                # per-file check: one wide directory must not stretch
                # the deadline across thousands of reads
                raise BackendError(
                    "SEARCH_TIMEOUT", f"search exceeded the {timeout_seconds}s deadline"
                )
            with contextlib.ExitStack() as guard:
                parent = guard.enter_context(open_directory_fd(root_fd, parts))
                data = _bounded_read(parent, name, max_file_bytes)
            if data is None:
                continue
            data = rg_binary_truncate(data)
            for match in scan_file(data, "/".join(full), needle, case_sensitive):
                matches.append(match)
                if len(matches) > limit:
                    truncated = True
                    break
            if truncated:
                break
    return matches[:limit], truncated


class DarwinWorkdirSession:
    """High-level session backed by the POSIX FD kernel on Darwin.

    Read channels are the same POSIX-shared call paths Linux uses; the
    differences live in search (no rg, no /proc) and replace-time metadata
    preservation (fcopyfile). The root FD is re-opened per operation.
    """

    def __init__(self, workdir: Workdir):
        self._workdir = workdir

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
        with self._root(resolved) as root:
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
        # open-and-close the search/find root so PATH/NOT_DIRECTORY errors
        # surface from this call, not from deep inside the walk
        with self._root(resolved) as root:
            with open_directory_fd(root, resolved.rel_parts):
                pass

    # ---- mutation channels ----

    def create_file(
        self, resolved: ResolvedPath, content: str, *, max_write_bytes: int
    ) -> CreateTextFileResult:
        return mutations.create_text_file(resolved, content, max_write_bytes=max_write_bytes)

    def replace_file(
        self,
        resolved: ResolvedPath,
        expected_revision: str,
        edits: list[TextEdit],
        *,
        max_write_bytes: int,
        max_edits_per_call: int,
    ):
        return mutations.edit_text_file(
            resolved,
            expected_revision,
            edits,
            max_write_bytes=max_write_bytes,
            max_edits_per_call=max_edits_per_call,
            preserve_metadata=preserve_metadata_darwin,
        )

    def delete_file(self, resolved: ResolvedPath, expected_revision: str) -> DeleteFileResult:
        return mutations.delete_file(resolved, expected_revision)

    def create_binary_file(
        self, resolved: ResolvedPath, data: bytes, *, max_binary_bytes: int
    ):
        return mutations.create_binary_file(resolved, data, max_binary_bytes=max_binary_bytes)

    def replace_binary_file(
        self,
        resolved: ResolvedPath,
        data: bytes,
        expected_revision: str,
        *,
        max_binary_bytes: int,
    ):
        return mutations.replace_binary_file(
            resolved,
            data,
            expected_revision,
            max_binary_bytes=max_binary_bytes,
            preserve_metadata=preserve_metadata_darwin,
        )

    def create_directory(self, resolved: ResolvedPath) -> CreateDirectoryResult:
        return mutations.create_directory(resolved)

    def delete_directory(
        self, resolved: ResolvedPath, expected_revision: str
    ) -> DeleteDirectoryResult:
        return mutations.delete_directory(resolved, expected_revision)


class DarwinBackend:
    """The Darwin filesystem kernel behind the session contract."""

    def __init__(self) -> None:
        # The gate is checked again here so a backend obtained without
        # going through get_backend cannot bypass the platform contract.
        ensure_supported_darwin()

    def open_session(self, workdir: Workdir) -> DarwinWorkdirSession:
        return DarwinWorkdirSession(workdir)


__all__ = ["DarwinBackend", "DarwinWorkdirSession", "preserve_metadata_darwin", "run_search"]

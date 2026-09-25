"""Read-only filesystem operations over FD-based traversal.

Everything here performs live reads against the current filesystem — no
caching, no indexing. Limits are enforced by the caller (tools layer).
Object identity is anchored on directory/file descriptors (see fdio.py),
so concurrent host-side renames cannot redirect access.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import fnmatch
import os
import stat as stat_module

from . import fdio
from .fdio import open_directory_fd, stat_final
from .models import EntryInfo, StatFileResult
from .mutations import compute_revision
from .paths import PathSecurityError, ResolvedPath, UnsupportedFileTypeError

_ENTRY_TYPE_FILE = "file"
_ENTRY_TYPE_DIR = "directory"
_ENTRY_TYPE_SYMLINK = "symlink"
_ENTRY_TYPE_OTHER = "other"


def _rfc3339_utc(mtime: float | None) -> str | None:
    if mtime is None:
        return None
    return _dt.datetime.fromtimestamp(mtime, tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_type(mode: int) -> str:
    """Map a stat mode to the entry type exposed to agents."""
    if stat_module.S_ISLNK(mode):
        return _ENTRY_TYPE_SYMLINK
    if stat_module.S_ISDIR(mode):
        return _ENTRY_TYPE_DIR
    if stat_module.S_ISREG(mode):
        return _ENTRY_TYPE_FILE
    # FIFO, socket, block/char device
    return _ENTRY_TYPE_OTHER


def _entry_visible(name: str, rel: str, resolved: ResolvedPath) -> bool:
    """One filter used by list/find: hidden + deny, from the same policy."""
    if not resolved.allow_hidden and name.startswith("."):
        return False
    segs = (*resolved.rel_parts, name) if not rel else (*tuple(rel.split("/")), name)
    if resolved.deny_policy.is_denied(segs):
        return False
    return True


def check_supported_file_type(resolved: ResolvedPath) -> None:
    """Reject anything that is not a regular file or directory (by FD)."""
    from .fdio import stat_final

    with _root_fd(resolved) as root_fd:
        st = stat_final(root_fd, resolved.rel_parts)
    if not (stat_module.S_ISREG(st.st_mode) or stat_module.S_ISDIR(st.st_mode)):
        raise UnsupportedFileTypeError()


def _root_fd(resolved: ResolvedPath):
    """Root-FD context manager for a resolved workdir path (fdio.root_fd)."""
    return fdio.root_fd(str(resolved.workdir.container_path))


def list_directory(
    resolved: ResolvedPath, *, offset: int, limit: int
) -> tuple[list[EntryInfo], bool]:
    """List one directory, sorted by name, with offset/limit pagination.

    Single scandir pass over a directory FD: name filter + immediate
    lstat-style stat (follow_symlinks=False) in the same scan, then sort,
    then paginate. Entries that vanish mid-scan are skipped entirely.
    """
    entries: list[EntryInfo] = []
    with contextlib.ExitStack() as stack:
        root = stack.enter_context(_root_fd(resolved))
        dir_fd = stack.enter_context(open_directory_fd(root, resolved.rel_parts))
        rel = resolved.rel_path
        with os.scandir(dir_fd) as it:
            for e in it:
                name = e.name
                if not _entry_visible(name, rel, resolved):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    # vanished between scandir and stat — skip
                    continue
                etype = entry_type(st.st_mode)
                entries.append(
                    EntryInfo(
                        name=name,
                        path=f"{rel}/{name}" if rel else name,
                        type=etype,
                        size=st.st_size if etype == _ENTRY_TYPE_FILE else None,
                        modified_at=_rfc3339_utc(st.st_mtime),
                    )
                )
    entries.sort(key=lambda x: x.name)
    has_more = offset + limit < len(entries)
    return entries[offset : offset + limit], has_more


def stat_file(resolved: ResolvedPath) -> StatFileResult:
    """lstat the final component through FD traversal (no symlink follow)."""
    with contextlib.ExitStack() as stack:
        root = stack.enter_context(_root_fd(resolved))
        st = stat_final(root, resolved.rel_parts)
    etype = entry_type(st.st_mode)
    mime_type = None
    if etype == _ENTRY_TYPE_FILE:
        import mimetypes

        mime_type = (
            mimetypes.guess_type(resolved.container_path.name)[0] or "application/octet-stream"
        )
    return StatFileResult(
        workdir=resolved.workdir.alias,
        path=resolved.rel_path,
        type=etype,
        size=st.st_size if etype == _ENTRY_TYPE_FILE else None,
        modified_at=_rfc3339_utc(st.st_mtime),
        mime_type=mime_type,
        revision=compute_revision(st),
    )


def find_files(
    resolved: ResolvedPath,
    *,
    pattern: str,
    limit: int,
    max_walk_entries: int,
) -> tuple[list[str], bool]:
    """Recursively find files matching a glob pattern under a directory.

    Directory traversal walks child directories by FD (O_NOFOLLOW), so
    symlinked directories are never entered. Stops as soon as ``limit``
    matches are found or ``max_walk_entries`` entries have been visited.
    Returns (relative paths, truncated).

    truncated=true means the scan stopped early due to a limit — either
    ``limit`` matches were collected or ``max_walk_entries`` was hit — so
    the result set is not guaranteed to be complete.
    """
    root = fdio.open_root(str(resolved.workdir.container_path))
    matches: list[str] = []
    visited = 0
    truncated = False
    # Keep only relative paths on the DFS stack. Opening every sibling
    # directory before visiting it makes peak FD usage grow with directory
    # width; reopening one path at a time keeps it bounded by traversal depth.
    stack: list[tuple[str, ...]] = [resolved.rel_parts]
    first = True
    try:
        while stack:
            rel_parts = stack.pop()
            try:
                with open_directory_fd(root, rel_parts) as current_fd:
                    child_infos: list[tuple[str, int]] = []
                    with os.scandir(current_fd) as it:
                        for child in it:
                            try:
                                st = child.stat(follow_symlinks=False)
                            except OSError:
                                continue  # vanished mid-scan
                            child_infos.append((child.name, st.st_mode))
            except (FileNotFoundError, NotADirectoryError):
                if first:
                    raise
                continue
            except PathSecurityError as exc:
                if first or exc.code == "RESOURCE_EXHAUSTED":
                    raise
                continue
            first = False

            child_infos.sort(key=lambda c: c[0])
            for name, mode in child_infos:
                visited += 1
                if visited > max_walk_entries:
                    truncated = True
                    return matches, truncated
                child_parts = (*rel_parts, name)
                if not resolved.allow_hidden and name.startswith("."):
                    continue
                if resolved.deny_policy.is_denied(child_parts):
                    continue
                child_rel = "/".join(child_parts)
                if stat_module.S_ISDIR(mode):
                    stack.append(child_parts)
                elif stat_module.S_ISREG(mode):
                    if fnmatch.fnmatchcase(name, pattern):
                        matches.append(child_rel)
                        if len(matches) >= limit:
                            truncated = True
                            return matches, truncated
    finally:
        try:
            os.close(root)
        except OSError:
            pass
    return matches, truncated

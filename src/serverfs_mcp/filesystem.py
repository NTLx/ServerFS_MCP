"""Read-only filesystem operations.

Everything here performs live reads against the current filesystem — no
caching, no indexing. Limits are enforced by the caller (tools layer).
"""

from __future__ import annotations

import datetime as _dt
import os
import stat as stat_module
from pathlib import Path

from .models import EntryInfo, StatFileResult
from .paths import PathSecurityError, ResolvedPath, UnsupportedFileTypeError

_ENTRY_TYPE_FILE = "file"
_ENTRY_TYPE_DIR = "directory"
_ENTRY_TYPE_SYMLINK = "symlink"


def _rfc3339_utc(mtime: float | None) -> str | None:
    if mtime is None:
        return None
    return _dt.datetime.fromtimestamp(mtime, tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_type(mode: int) -> str:
    if stat_module.S_ISLNK(mode):
        return _ENTRY_TYPE_SYMLINK
    if stat_module.S_ISDIR(mode):
        return _ENTRY_TYPE_DIR
    return _ENTRY_TYPE_FILE


def check_supported_file_type(resolved: ResolvedPath) -> None:
    """Reject anything that is not a regular file or directory.

    Must be called before opening a path so FIFO/socket/device reads can never
    block. Uses lstat (no symlink following; symlinks are already rejected by
    the path layer).
    """
    try:
        st = os.lstat(resolved.container_path)
    except FileNotFoundError:
        raise
    if not (stat_module.S_ISREG(st.st_mode) or stat_module.S_ISDIR(st.st_mode)):
        raise UnsupportedFileTypeError()


def list_directory(
    resolved: ResolvedPath, *, offset: int, limit: int
) -> tuple[list[EntryInfo], bool]:
    """List one directory, sorted by name, with offset/limit pagination.

    Returns (entries, has_more). Symlinks are reported as type "symlink"
    without following them or revealing targets. Hidden and denied entries
    are filtered here (the path layer guards direct access to them).
    """
    root = resolved.container_path
    try:
        st = os.stat(root)
    except FileNotFoundError:
        raise
    if not stat_module.S_ISDIR(st.st_mode):
        raise NotADirectoryError(f"NOT_A_DIRECTORY: {resolved.rel_path} is not a directory")

    names: list[str] = []
    with os.scandir(root) as it:
        for entry in it:
            name = entry.name
            if _hidden_component(name):
                continue
            if _denied_entry(name):
                continue
            names.append(name)

    names.sort()
    selected = names[offset : offset + limit]
    has_more = offset + limit < len(names)

    entries: list[EntryInfo] = []
    with os.scandir(root) as it:
        stat_cache = {e.name: e for e in it}
    for name in selected:
        entry = stat_cache[name]
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            # entry vanished between the two scandir passes — report without stat
            entries.append(
                EntryInfo(
                    name=name,
                    path=f"{resolved.rel_path}/{name}" if resolved.rel_path else name,
                    type=_ENTRY_TYPE_SYMLINK,
                )
            )
            continue
        etype = _entry_type(st.st_mode)
        entries.append(
            EntryInfo(
                name=name,
                path=f"{resolved.rel_path}/{name}" if resolved.rel_path else name,
                type=etype,
                size=st.st_size if etype == _ENTRY_TYPE_FILE else None,
                modified_at=_rfc3339_utc(st.st_mtime),
            )
        )
    return entries, has_more


def _hidden_component(name: str) -> bool:
    return name.startswith(".")


def _denied_entry(name: str) -> bool:
    # basename-level deny for listing; directory-level deny (.ssh etc.) is
    # handled because such dirs are themselves hidden and filtered above.
    from .paths import DEFAULT_DENY_BASENAMES, DEFAULT_DENY_GLOBS

    if name in DEFAULT_DENY_BASENAMES:
        return True
    return any(fnmatch_name(name, g) for g in DEFAULT_DENY_GLOBS)


def fnmatch_name(name: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(name, pattern)


def stat_file(resolved: ResolvedPath) -> StatFileResult:
    """Stat a single path (lstat semantics: symlinks reported as symlinks)."""
    try:
        st = os.lstat(resolved.container_path)
    except FileNotFoundError:
        raise
    etype = _entry_type(st.st_mode)
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
    )


def find_files(
    resolved: ResolvedPath,
    *,
    pattern: str,
    limit: int,
    max_walk_entries: int,
) -> tuple[list[str], bool]:
    """Recursively find files matching a glob pattern under a directory.

    Uses os.scandir + explicit stack; never follows symlinked directories.
    Stops as soon as ``limit`` matches are found or ``max_walk_entries``
    entries have been visited. Returns (relative paths, truncated).
    """
    root = resolved.container_path
    matches: list[str] = []
    visited = 0
    truncated = False
    stack: list[tuple[Path, str]] = [(root, resolved.rel_path)]

    while stack:
        current, rel = stack.pop()
        try:
            with os.scandir(current) as it:
                children = list(it)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            continue
        children.sort(key=lambda e: e.name)
        for child in children:
            visited += 1
            if visited > max_walk_entries:
                truncated = True
                return matches, truncated
            name = child.name
            if _hidden_component(name):
                continue
            if _denied_entry(name):
                continue
            child_rel = f"{rel}/{name}" if rel else name
            try:
                st = child.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat_module.S_ISDIR(st.st_mode):
                stack.append((child.path, child_rel))
            elif stat_module.S_ISREG(st.st_mode):
                if fnmatch_name(name, pattern):
                    matches.append(child_rel)
                    if len(matches) >= limit:
                        return matches, False
    return matches, truncated


def iter_text_lines(resolved: ResolvedPath) -> object:
    """Open a validated file for line iteration (binary mode, caller decodes)."""
    return open(resolved.container_path, "rb")


# re-exported for the tools layer
__all__ = [
    "PathSecurityError",
    "check_supported_file_type",
    "find_files",
    "list_directory",
    "stat_file",
]

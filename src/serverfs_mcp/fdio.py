"""FD-based filesystem traversal primitives (Linux-only).

All filesystem access in ServerFS walks path components via directory file
descriptors using openat semantics:

    os.open(component, O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC, dir_fd=fd)

This closes the classic lstat→open TOCTOU window: component identity and
symlink rejection are decided atomically by the kernel at open time, and
once a file FD is held, stat/read operate on the exact object that was
opened — a concurrent rename/replace cannot redirect either.

Errors are mapped to the coded exceptions from paths.py (or the builtin
FileNotFoundError/NotADirectoryError) so the tool layer can surface
agent-recoverable codes.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat as stat_module

from .paths import (
    RESERVED_TEMP_PREFIX,
    PathSecurityError,
    SymlinkNotAllowedError,
    UnsupportedFileTypeError,
)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


def _map_open_error(exc: OSError, name: str, *, dir_fd: int | None = None) -> None:
    """Re-raise a failed openat as a coded, agent-safe exception.

    ENOTDIR is ambiguous on Linux: opening a symlink with
    O_DIRECTORY|O_NOFOLLOW yields ENOTDIR, not ELOOP. When dir_fd is
    available we lstat purely to CLASSIFY the error (the open already
    failed, so this adds no TOCTOU exposure).
    """
    if exc.errno == errno.ELOOP:
        raise SymlinkNotAllowedError() from exc
    if exc.errno == errno.ENOENT:
        raise FileNotFoundError(name) from exc
    if exc.errno == errno.ENOTDIR and dir_fd is not None:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            pass
        else:
            if stat_module.S_ISLNK(st.st_mode):
                raise SymlinkNotAllowedError() from exc
    if exc.errno in (errno.ENOTDIR, errno.EISDIR):
        raise NotADirectoryError(name) from exc
    if exc.errno == errno.ENXIO:
        # e.g. opening a UNIX socket
        raise UnsupportedFileTypeError() from exc
    if exc.errno in (errno.EACCES, errno.EPERM):
        raise PathSecurityError("permission denied") from exc
    raise PathSecurityError(exc.strerror or "cannot access path") from exc


def open_root(path: str) -> int:
    """Open the workdir root directory (a bind mount, never a symlink).

    This is the one path opened by name: the trusted anchor from
    configuration, carrying no request input.
    """
    try:
        return os.open(path, _DIR_FLAGS | os.O_CLOEXEC)
    except OSError as exc:
        _map_open_error(exc, path)


@contextlib.contextmanager
def root_fd(path: str):
    """Context-managed open_root: closes the descriptor on exit.

    Every module that needs the workdir root anchors on this, so the open
    flags and the error mapping exist exactly once.
    """
    fd = open_root(path)
    try:
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def walk_parent_dirs(root_fd: int, rel_parts: tuple[str, ...]):
    """Yield the parent directory FD for the final component of rel_parts.

    Every component is opened with O_DIRECTORY|O_NOFOLLOW: a symlink (or a
    non-directory) anywhere on the parent chain raises before the caller
    touches the final component. The final component itself is NOT opened
    here — callers decide how to handle it (open file, open dir, stat).
    """
    current = root_fd
    opened: list[int] = []
    try:
        for seg in rel_parts:
            try:
                fd = os.open(seg, _DIR_FLAGS | os.O_CLOEXEC, dir_fd=current)
            except OSError as exc:
                _map_open_error(exc, seg, dir_fd=current)
            opened.append(fd)
            current = fd
        yield current
    finally:
        for fd in reversed(opened):
            os.close(fd)


@contextlib.contextmanager
def open_dir_at(parent_fd: int, name: str):
    """Open one name relative to an already-open parent FD as a directory.

    O_DIRECTORY|O_NOFOLLOW: a symlink is refused by the kernel (reported as
    ENOTDIR, classified by a supplementary lstat after the failed open — the
    access itself is already refused, so this adds no race).
    """
    try:
        fd = os.open(name, _DIR_FLAGS | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as exc:
        _map_open_error(exc, name, dir_fd=parent_fd)
    try:
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def open_directory_fd(root_fd: int, rel_parts: tuple[str, ...]):
    """Open the FULL rel_parts as a directory (final component included)."""
    if not rel_parts:
        # borrow the root fd via dup: same directory object, independent
        # descriptor the caller may close (no re-resolution, no follow)
        fd = os.dup(root_fd)
        try:
            yield fd
        finally:
            os.close(fd)
        return
    *parents, final = rel_parts
    with walk_parent_dirs(root_fd, tuple(parents)) as parent_fd:
        with open_dir_at(parent_fd, final) as fd:
            yield fd


@contextlib.contextmanager
def open_regular_at(parent_fd: int, name: str):
    """Open one name relative to an already-open parent FD as a regular file.

    Read-only, O_NOFOLLOW, O_NONBLOCK (a FIFO open can never block).
    Regularity is verified by fstat on the yielded fd, so the caller works
    on exactly the object that was opened — a concurrent rename cannot
    redirect it. Directory → NotADirectoryError, symlink →
    SymlinkNotAllowedError, other types → UnsupportedFileTypeError.
    """
    try:
        fd = os.open(name, _FILE_FLAGS | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as exc:
        _map_open_error(exc, name, dir_fd=parent_fd)
    try:
        st = os.fstat(fd)
        if stat_module.S_ISDIR(st.st_mode):
            raise NotADirectoryError(name)
        if not stat_module.S_ISREG(st.st_mode):
            raise UnsupportedFileTypeError()
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def open_file_fd(root_fd: int, rel_parts: tuple[str, ...]):
    """Open the final component as a regular file, read-only, no follow.

    Thin wrapper over open_regular_at: the parent chain is walked by FD
    first, then the final component is opened relative to it.
    """
    if not rel_parts:
        raise NotADirectoryError("workdir root is a directory")
    *parents, final = rel_parts
    with walk_parent_dirs(root_fd, tuple(parents)) as parent_fd:
        with open_regular_at(parent_fd, final) as fd:
            yield fd


def stat_final(root_fd: int, rel_parts: tuple[str, ...]) -> os.stat_result:
    """lstat the final component through FD traversal (no symlink follow).

    The final component being a symlink is reported as S_ISLNK (callers
    surface type="symlink"); a symlink anywhere in the PARENT chain raises
    SymlinkNotAllowedError.
    """
    if not rel_parts:
        return os.fstat(root_fd)
    *parents, final = rel_parts
    with walk_parent_dirs(root_fd, tuple(parents)) as parent_fd:
        try:
            return os.stat(final, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise FileNotFoundError(final) from None
        except OSError as exc:
            _map_open_error(exc, final)


def proc_fd_path(fd: int) -> str:
    """/proc/self/fd/N — used as a child-process cwd so rg operates on the
    exact directory object we validated (pass_fds keeps it alive)."""
    return f"/proc/self/fd/{fd}"


# ---- mutation primitives (all dir_fd-relative, never re-resolved) ----


def stat_at(parent_fd: int, name: str) -> os.stat_result:
    """lstat one name relative to an already-open parent FD (no follow)."""
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        _map_open_error(exc, name, dir_fd=parent_fd)


def create_temp_at(parent_fd: int) -> tuple[str, int]:
    """Create a reserved same-directory temp file; return (name, fd).

    O_EXCL|O_NOFOLLOW means the name is new and cannot be a symlink. The
    name lives in the reserved namespace, so no channel can expose it even
    if this process dies before cleaning up.
    """
    name = RESERVED_TEMP_PREFIX + secrets.token_hex(8)
    fd = os.open(name, _TEMP_FLAGS | os.O_CLOEXEC, 0o666, dir_fd=parent_fd)
    return name, fd


def unlink_at(parent_fd: int, name: str) -> None:
    """Best-effort unlink; never raises (cleanup must not mask the error)."""
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        pass


def fsync_directory(fd: int) -> None:
    """Persist a directory entry change (create/rename/unlink durability)."""
    os.fsync(fd)

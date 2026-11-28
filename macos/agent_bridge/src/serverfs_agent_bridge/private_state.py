"""macOS private-state authorization.

Keep the existing caller-specific error vocabulary and state layout.
Use the native UID, descriptor and mode-bit contract; retain existing shared-group rules.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

WINDOWS = False  # Local compatibility flag for remaining callers.

__all__ = [
    "DirectoryMessages",
    "WINDOWS",
    "apply_private_file_mode",
    "ensure_private_directory",
    "ensure_private_file",
    "ensure_private_file_handle",
    "open_flags",
    "opened_file_is_private",
    "require_regular_file",
    "state_entry_is_regular",
    "verify_private_file",
]


@dataclass(frozen=True)
class DirectoryMessages:
    """The frozen failure vocabulary of one state directory."""

    not_a_directory: str
    not_owned: str
    private_mode: str | None = None
    shared_mode: str | None = None
    group_change_failed: str | None = None


def open_flags(access: str, *, create: bool = False, exclusive: bool = False) -> int:
    """The flags every Bridge state open shares, spelled the way this platform spells them.

    POSIX refuses an inherited or executed-over handle with ``O_CLOEXEC`` and a followed link
    with ``O_NOFOLLOW``; neither name exists on Windows, where the equivalent of the first is
    ``O_NOINHERIT`` and the second is the path check beside the open (§28). Call sites ask for
    an access mode instead of OR-ing platform constants, which is what §24 asks for.
    """
    mode = {"read": os.O_RDONLY, "write": os.O_WRONLY, "read-write": os.O_RDWR}[access]
    flags = (
        mode
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if create:
        flags |= os.O_CREAT
    if exclusive:
        flags |= os.O_EXCL
    return flags


def apply_private_file_mode(fd: int, *, mode: int, shared_gid: int | None = None) -> None:
    """Apply native mode and optional group to an open descriptor."""
    os.fchmod(fd, mode)
    if shared_gid is not None:
        os.fchown(fd, -1, shared_gid)


def ensure_private_file_handle(fd: int, *, mode: int, shared_gid: int | None = None) -> None:
    """Confirm a freshly opened descriptor is a regular file and apply its private mode."""
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise ValueError("private state path must be a regular file")
    apply_private_file_mode(fd, mode=mode, shared_gid=shared_gid)


def ensure_private_directory(
    path: Path,
    *,
    mode: int,
    messages: DirectoryMessages,
    shared_gid: int | None = None,
    parents: bool = False,
) -> None:
    """Create one Bridge state directory, or verify the one that already exists."""
    try:
        directory_stat = path.lstat()
    except FileNotFoundError:
        if parents:
            # Create each missing ancestor with the intended private mode.
            missing: list[Path] = []
            current = path.parent
            while not current.exists():
                missing.append(current)
                parent = current.parent
                if parent == current:
                    break
                current = parent
            for directory in reversed(missing):
                directory.mkdir(mode=mode)
                os.chmod(directory, mode)
            path.mkdir(mode=mode)
        else:
            path.mkdir(mode=mode)
        directory_stat = path.lstat()
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise ValueError(messages.not_a_directory)
    if directory_stat.st_uid != os.getuid():
        raise ValueError(messages.not_owned)
    if shared_gid is None:
        if directory_stat.st_mode & 0o077:
            raise ValueError(messages.private_mode or messages.not_owned)
    else:
        if directory_stat.st_mode & 0o027:
            raise ValueError(messages.shared_mode or messages.not_owned)
        if directory_stat.st_gid != shared_gid:
            try:
                os.chown(path, -1, shared_gid)
            except OSError as exc:
                if messages.group_change_failed is None:
                    raise
                raise ValueError(messages.group_change_failed) from exc
    os.chmod(path, mode)


def ensure_private_file(
    path: Path, *, mode: int, not_regular: str, shared_gid: int | None = None
) -> None:
    """Create one private state file, or apply the contract to the one that already exists.

    A *directory* that already exists is verified and never re-secured (§25), but a state file
    is created idempotently because a Bridge restart legitimately re-opens its own lease and
    lock files — the same asymmetry the Linux contract has always had.
    """
    require_regular_file(path, not_regular=not_regular)
    fd = os.open(path, open_flags("read-write", create=True), mode)
    try:
        ensure_private_file_handle(fd, mode=mode, shared_gid=shared_gid)
    finally:
        os.close(fd)


def require_regular_file(path: Path, *, not_regular: str) -> None:
    """Confirm a path the Bridge will use is a real regular file, not a link or reparse point."""
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(not_regular)


def protect_existing_file(path: Path, *, mode: int, not_private: str) -> None:
    """Preserve the existing mode repair for Bridge-owned SQLite files."""
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        return


def verify_private_file(path: Path, *, not_regular: str, not_private: str) -> None:
    """Re-validate an object the Bridge did not necessarily create (SQLite sidecars, §29)."""
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(not_regular)
    if file_stat.st_uid != os.getuid() or file_stat.st_mode & 0o077:
        raise ValueError(not_private)


def opened_file_is_private(path: Path, opened: os.stat_result) -> bool:
    """Check the opened native descriptor owner, mode and regular-file type."""
    return (
        not stat.S_ISLNK(opened.st_mode)
        and stat.S_ISREG(opened.st_mode)
        and opened.st_uid == os.getuid()
        and not opened.st_mode & 0o077
    )


def state_entry_is_regular(stat_result: os.stat_result) -> bool:
    """Whether an ``lstat`` of a state path names a real file, not a link or reparse point."""
    return not stat.S_ISLNK(stat_result.st_mode) and stat.S_ISREG(stat_result.st_mode)

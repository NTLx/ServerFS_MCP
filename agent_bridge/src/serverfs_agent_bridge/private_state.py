"""The §3 private-state authorization seam (§6.1, §24–§29).

One place decides whether a Bridge state object is private. Call sites keep their own frozen
error vocabulary — the exact strings the v0.6–v0.9 tests assert — by passing it in, while the
platform logic (POSIX owner and mode bits versus Windows owner SID, DACL and reparse state)
lives only here. §6.1 forbids restating that raw ACL logic in store.py, result_spool.py,
recovery.py and the transport.

Linux semantics are unchanged: a real directory, owned by the Bridge user, no group or world
access, and the mode repaired where the existing contract already repaired it.

Windows semantics:

- a new object is created with an explicit protected descriptor granting exactly the Bridge
  user, so the ``Everyone``/``Anonymous`` read grants of the default descriptor cannot survive;
- an existing object is verified, never silently re-secured (§25). The Bridge applies a DACL
  only at creation, so an object that already exists with another owner, an unprotected
  explicit DACL, or any trustee besides the Bridge user is refused. That is the boundary
  against an attacker who pre-plants a state path;
- a reparse point is refused on every security-relevant component (§28). A junction reports
  ``st_reparse_tag`` while ``S_ISLNK`` is false and ``is_dir()`` is true — and creating one
  needs no privilege — so the tag, not the POSIX mode bits, is the check;
- ``shared_gid`` is the Phase E group-readable model. It has no Windows equivalent in this
  contract, so a Windows process asked for it fails closed instead of guessing.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import BridgeError

WINDOWS = sys.platform == "win32"

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
        | getattr(os, "O_NOINHERIT", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if create:
        flags |= os.O_CREAT
    if exclusive:
        flags |= os.O_EXCL
    return flags


def apply_private_file_mode(fd: int, *, mode: int, shared_gid: int | None = None) -> None:
    """Apply the private mode to an open descriptor.

    Windows has no per-descriptor mode: the object inherited its DACL from the protected state
    directory at creation, and that inheritance is what §29 relies on for the SQLite
    ``-wal``/``-shm`` companions. The DACL and reparse checks therefore run on the path.
    """
    if WINDOWS:
        return
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
    if WINDOWS:
        _windows_ensure_directory(path, shared_gid=shared_gid, messages=messages, parents=parents)
        return
    try:
        directory_stat = path.lstat()
    except FileNotFoundError:
        if parents:
            path.mkdir(parents=True, exist_ok=True, mode=mode)
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


def _windows_ensure_directory(
    path: Path,
    *,
    shared_gid: int | None,
    messages: DirectoryMessages,
    parents: bool,
) -> None:
    from . import windows_security

    if shared_gid is not None:
        raise BridgeError(
            "BRIDGE_PLATFORM_UNSUPPORTED",
            "group-readable private state is a POSIX mechanism; Windows state is protected by "
            "an explicit owner DACL",
        )
    expected_sid = windows_security.current_token_owner_sid()
    sddl = windows_security.private_state_sddl(expected_sid)
    if parents:
        _windows_create_ancestors(path, sddl=sddl, messages=messages)
    _windows_refuse_reparse(path, messages.not_a_directory)
    # ``False`` means the directory was already there, which §25 makes a verification-only
    # case: an inherited descriptor is acceptable on an object we did not create, as long as
    # it grants nobody but the Bridge user.
    created = windows_security.create_private_directory(path, sddl)
    _assert_windows_private(
        windows_security.read_object_security(path), expected_sid, protected=created, path=path
    )
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise ValueError(messages.not_a_directory) from exc
    if not stat.S_ISDIR(stat_result.st_mode):
        raise ValueError(messages.not_a_directory)


def _windows_create_ancestors(path: Path, *, sddl: str, messages: DirectoryMessages) -> None:
    """Create every missing component with the same protected descriptor.

    A parent created with the default descriptor would leave an inheritable grant in place for
    everything below it, so the whole tree the Bridge creates gets the same explicit DACL.
    """
    from . import windows_security

    missing: list[Path] = []
    current = path.parent
    while True:
        # Reparpose is asked before existence, for the same reason as everywhere else: a dangling
        # reparse parent reports exists() == False and would otherwise be classified as merely
        # missing. It would then be walked *past* as if it were an ordinary absent component, and
        # the walk would stop one level higher — anchoring the Bridge's own tree inside somebody
        # else's directory, which is precisely the §28 junction case this loop exists to catch.
        _windows_refuse_reparse(current, messages.not_a_directory)
        if current.exists():
            # The walk stops at the first directory that already exists, and that directory is
            # exactly where a pre-planted junction can hide (§28): every component created below
            # it would land inside somebody else's tree, so the Bridge's own root is refused.
            break
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ValueError(messages.not_a_directory)
        current = parent
    for directory in reversed(missing):
        _windows_refuse_reparse(directory, messages.not_a_directory)
        windows_security.create_private_directory(directory, sddl)


def _windows_refuse_reparse(path: Path, not_a_directory: str) -> None:
    """Refuse a reparse point at ``path``, including one that dangles.

    The order is the whole point: ``is_reparse_point`` is asked first and nothing is gated behind
    ``exists()``. ``Path.exists()`` *follows* a link, so a dangling symlink — one whose target is
    absent — reports False while ``lstat`` still carries the reparse tag. A conjunction or an
    ``exists()`` pre-check therefore classifies exactly the cheapest object an attacker can plant,
    and the one that leaves no visible trace, as "nothing there". ``is_reparse_point`` already
    answers all three cases correctly: a genuinely absent path is False because ``lstat`` raises
    ``FileNotFoundError``, an existing reparse point is True, and an inspection failure is refused
    closed by raising.
    """
    from . import windows_security

    if windows_security.is_reparse_point(path):
        raise BridgeError("PRIVATE_STATE_UNSAFE", f"{not_a_directory} (reparse point)")


def _assert_windows_private(security, expected_sid: str, *, protected: bool, path=None) -> None:
    """§27: present, explicit where we created it, and granting the Bridge user and nobody else."""
    from . import windows_security

    if not security.dacl_present:
        raise BridgeError("PRIVATE_STATE_UNSAFE", "state object has no explicit DACL")
    if protected and not security.dacl_protected:
        raise BridgeError("PRIVATE_STATE_UNSAFE", "state object inherits an unexpected DACL")
    if security.owner_sid != expected_sid:
        raise BridgeError("PRIVATE_STATE_UNSAFE", "state object has another owner")
    banned = security.broad_trustee()
    if banned is not None:
        raise BridgeError("PRIVATE_STATE_UNSAFE", f"state DACL grants access to {banned}")
    for ace in security.aces:
        if ace["type"] != windows_security.ACE_ACCESS_ALLOWED:
            raise BridgeError("PRIVATE_STATE_UNSAFE", "state DACL contains a non-allow ACE")
        if ace["sid"] == expected_sid or ace["sid"] in windows_security.SYSTEM_TRUSTEES:
            continue
        # The trustee SID is machine-local (a group or account identity, never a credential)
        # and naming it is what makes a remote runner mismatch diagnosable.
        where = f" on {path}" if path is not None else ""
        raise BridgeError(
            "PRIVATE_STATE_UNSAFE",
            f"state DACL grants another trustee ({ace['sid']}); expected {expected_sid}{where}",
        )
    if not security.grants(expected_sid):
        raise BridgeError("PRIVATE_STATE_UNSAFE", "state DACL does not grant the Bridge user")


def ensure_private_file(
    path: Path, *, mode: int, not_regular: str, shared_gid: int | None = None
) -> None:
    """Create one private state file, or apply the contract to the one that already exists.

    A *directory* that already exists is verified and never re-secured (§25), but a state file
    is created idempotently because a Bridge restart legitimately re-opens its own lease and
    lock files — the same asymmetry the Linux contract has always had.
    """
    if WINDOWS:
        from . import windows_security

        if shared_gid is not None:
            raise BridgeError(
                "BRIDGE_PLATFORM_UNSUPPORTED",
                "group-readable private state is a POSIX mechanism",
            )
        expected_sid = windows_security.current_token_owner_sid()
        _windows_refuse_reparse(path, not_regular)
        existed = path.exists()
        windows_security.create_private_file(
            path, windows_security.private_state_sddl(expected_sid)
        )
        _assert_windows_private(
            windows_security.read_object_security(path),
            expected_sid,
            protected=not existed,
            path=path,
        )
        return
    require_regular_file(path, not_regular=not_regular)
    fd = os.open(path, open_flags("read-write", create=True), mode)
    try:
        ensure_private_file_handle(fd, mode=mode, shared_gid=shared_gid)
    finally:
        os.close(fd)


def require_regular_file(path: Path, *, not_regular: str) -> None:
    """Confirm a path the Bridge will use is a real regular file, not a link or reparse point."""
    if WINDOWS:
        from . import windows_security

        if windows_security.is_reparse_point(path):
            raise BridgeError("PRIVATE_STATE_UNSAFE", f"{not_regular} (reparse point)")
        if not path.exists():
            return
        if path.is_dir():
            raise ValueError(not_regular)
        return
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(not_regular)


def protect_existing_file(path: Path, *, mode: int, not_private: str) -> None:
    """Apply the file contract to an object the Bridge did not create (SQLite sidecars, §29).

    The Linux contract has always been to repair the mode of its own database files, so that
    is what this does there. Windows cannot repair without becoming the thing §25 forbids, so
    the sidecar's inherited owner SID and DACL are verified instead and anything unexpected is
    refused.
    """
    if WINDOWS:
        # A reparse point is refused before existence is consulted, so a dangling one cannot be
        # mistaken for a sidecar that is merely not there yet (§28, and the same ordering the
        # directory and renderer paths use).
        from . import windows_security

        if windows_security.is_reparse_point(path):
            raise BridgeError("PRIVATE_STATE_UNSAFE", "state database sidecar (reparse point)")
        if not path.exists():
            return
        verify_private_file(
            path,
            not_regular="state database sidecar must be a regular file",
            not_private=not_private,
        )
        return
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        return


def verify_private_file(path: Path, *, not_regular: str, not_private: str) -> None:
    """Re-validate an object the Bridge did not necessarily create (SQLite sidecars, §29)."""
    if WINDOWS:
        from . import windows_security

        expected_sid = windows_security.current_token_owner_sid()
        _windows_refuse_reparse(path, not_regular)
        _assert_windows_private(
            windows_security.read_object_security(path), expected_sid, protected=False
        )
        return
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(not_regular)
    if file_stat.st_uid != os.getuid() or file_stat.st_mode & 0o077:
        raise ValueError(not_private)


def opened_file_is_private(path: Path, opened: os.stat_result) -> bool:
    """Whether a descriptor the caller just opened is its own private regular file.

    POSIX reads that answer from the descriptor itself — owner UID and mode bits are what the
    kernel recorded, and ``O_NOFOLLOW`` already refused a link at the open. A Windows ``fstat``
    carries no ACL, and Python has no ``O_NOFOLLOW`` there, so the path beside the descriptor
    is verified instead: owner SID, protected DACL and the reparse tag (§28, §29).
    """
    if WINDOWS:
        if not stat.S_ISREG(opened.st_mode):
            return False
        try:
            verify_private_file(
                path, not_regular="state file is unsafe", not_private="state file is unsafe"
            )
        except (BridgeError, ValueError):
            return False
        return True
    return (
        not stat.S_ISLNK(opened.st_mode)
        and stat.S_ISREG(opened.st_mode)
        and opened.st_uid == os.getuid()
        and not opened.st_mode & 0o077
    )


def state_entry_is_regular(stat_result: os.stat_result) -> bool:
    """Whether an ``lstat`` of a state path names a real file, not a link or reparse point."""
    if WINDOWS:
        from . import windows_security

        return windows_security.regular_entry(stat_result)
    return not stat.S_ISLNK(stat_result.st_mode) and stat.S_ISREG(stat_result.st_mode)

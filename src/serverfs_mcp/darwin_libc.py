"""Minimal Darwin libc bindings for primitives Python does not expose.

Every binding here was verified against the real macOS 27 / arm64 system in
``docs/phase-0-macos27-arm64-capability-probe-2026-10.md``:

- ``fcopyfile(src_fd, dst_fd, NULL, COPYFILE_METADATA)`` carries POSIX mode,
  user xattrs AND extended ACLs onto the destination inode without touching
  its content (probe 0D) — the Darwin replacement-metadata primitive;
- ``getpeereid`` returns the authoritative peer euid/egid over AF_UNIX
  (probe 0E) — added here in Phase D;
- ``confstr(_CS_DARWIN_USER_TEMP_DIR)`` resolves the OS-provided per-user
  runtime directory (probe 0G) — added here in Phase D.

The module imports cleanly only on Darwin; callers gate on
``sys.platform == "darwin"`` before importing it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import stat as stat_module
from pathlib import Path

# copyfile.h: COPYFILE_STAT (1<<0), COPYFILE_ACL (1<<1), COPYFILE_XATTR
# (1<<2); COPYFILE_METADATA is exactly those three bits — everything about
# the file EXCEPT its data.
_COPYFILE_STAT = 1 << 0
_COPYFILE_ACL = 1 << 1
_COPYFILE_XATTR = 1 << 2
COPYFILE_METADATA = _COPYFILE_STAT | _COPYFILE_ACL | _COPYFILE_XATTR

# confstr name for the OS-provided per-user runtime/temp directory (0G).
_CS_DARWIN_USER_TEMP_DIR = 65536

#: sun_path limit on Darwin including the terminating NUL (kernel constant,
#: probe 0G); a longer endpoint must be refused before bind.
SOCKET_PATH_MAX_BYTES = 103

#: The runtime subdirectory the Darwin Bridge socket lives in (§11 D2).
RUNTIME_DIRECTORY = "serverfs-agent-bridge-v1"
SOCKET_NAME = "bridge.sock"

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.fcopyfile.restype = ctypes.c_int
_libc.fcopyfile.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]


class FcopyfileError(OSError):
    """fcopyfile refused or failed; the caller must fail the publication."""


def fcopyfile_metadata(src_fd: int, dst_fd: int) -> None:
    """Copy mode + xattrs + ACLs from src_fd onto dst_fd, content untouched.

    Raises FcopyfileError on any failure so the caller can abandon the
    replacement BEFORE publication (no silent metadata loss).
    """
    rc = _libc.fcopyfile(
        ctypes.c_int(src_fd), ctypes.c_int(dst_fd), None, ctypes.c_int(COPYFILE_METADATA)
    )
    if rc != 0:
        errno_value = ctypes.get_errno()
        raise FcopyfileError(errno_value, f"fcopyfile(COPYFILE_METADATA) failed rc={rc}")


def confstr_darwin_user_temp_dir() -> str:
    """The OS-provided per-user runtime directory (probe 0G), via confstr."""
    _libc.confstr.restype = ctypes.c_size_t
    _libc.confstr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t]
    buf = ctypes.create_string_buffer(1024)
    length = _libc.confstr(_CS_DARWIN_USER_TEMP_DIR, buf, 1024)
    if length == 0:
        raise RuntimeError("the Darwin user runtime dir is unavailable")
    value = buf.value.decode("utf-8")
    if not value:
        raise RuntimeError("the Darwin user runtime dir is unavailable")
    return value


def darwin_runtime_dir() -> Path:
    """Validated per-user Darwin runtime directory (§11 D2).

    The confstr value is OS-provided per-user, but it is still verified
    before use: a real directory, owned by the current UID, no group or
    world write bits, and not a symlink. An inherited ``$TMPDIR`` string is
    never trusted as the security boundary.
    """
    path = Path(confstr_darwin_user_temp_dir())
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError("the Darwin user runtime dir failed its safety checks") from exc
    if (
        stat_module.S_ISLNK(info.st_mode)
        or not stat_module.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise RuntimeError("the Darwin user runtime dir failed its safety checks")
    return path


def darwin_bridge_socket_path() -> Path:
    """The deterministic Darwin Bridge endpoint, within the sun_path budget."""
    path = darwin_runtime_dir() / RUNTIME_DIRECTORY / SOCKET_NAME
    if len(os.fsencode(str(path))) > SOCKET_PATH_MAX_BYTES:
        raise RuntimeError("the derived bridge socket path exceeds the sun_path limit")
    return path


def getpeer_eid(fd: int) -> tuple[int, int]:
    """Authoritative peer (euid, egid) of an AF_UNIX connection (probe 0E)."""
    _libc.getpeereid.restype = ctypes.c_int
    _libc.getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    uid = ctypes.c_uint32(0)
    gid = ctypes.c_uint32(0)
    rc = _libc.getpeereid(ctypes.c_int(fd), ctypes.byref(uid), ctypes.byref(gid))
    if rc != 0:
        raise OSError(ctypes.get_errno(), "getpeereid failed")
    return uid.value, gid.value

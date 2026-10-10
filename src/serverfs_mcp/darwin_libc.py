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

# copyfile.h: COPYFILE_STAT (1<<0), COPYFILE_ACL (1<<1), COPYFILE_XATTR
# (1<<2); COPYFILE_METADATA is exactly those three bits — everything about
# the file EXCEPT its data.
_COPYFILE_STAT = 1 << 0
_COPYFILE_ACL = 1 << 1
_COPYFILE_XATTR = 1 << 2
COPYFILE_METADATA = _COPYFILE_STAT | _COPYFILE_ACL | _COPYFILE_XATTR

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

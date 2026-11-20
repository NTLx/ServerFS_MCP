"""W2g installer-only private DACL setter for current-user Windows environments.

This is intentionally outside the versioned W2f Product Wheel. It reuses
Product's trusted private-file setter and uses DACL-only Win32 calls for
directories, where OI+CI inheritance is mandatory for uv child files.
It never changes owner, SACL, task registration, or an existing credential.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path


class W2gAclError(Exception):
    """Sanitized installation-only ACL error."""


def protect_private_directory(path: Path, sid: str) -> None:
    """Replace only a newly staged directory's DACL with protected OI/CI ACL."""
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

    if sys.platform != "win32" or os.name != "nt":
        raise W2gAclError("native Windows only")
    _no_reparse_components(path)
    if not path.is_dir():
        raise W2gAclError("target is not a directory")

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    get_dacl = advapi.GetSecurityDescriptorDacl
    get_dacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_dacl.restype = wintypes.BOOL
    set_security = advapi.SetNamedSecurityInfoW
    set_security.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    set_security.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    # Inheritance is essential for uv-created child directories and files.
    # No inherited or other-user ACE survives publication.
    sddl = f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    descriptor = ctypes.c_void_p()
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise W2gAclError("private directory DACL creation failed")
    try:
        present = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        defaulted = wintypes.BOOL()
        if (
            not get_dacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            )
            or not present.value
            or not dacl.value
        ):
            raise W2gAclError("private directory DACL cannot be validated")
        # SE_FILE_OBJECT + PROTECTED_DACL | DACL_SECURITY_INFORMATION only.
        if set_security(str(path), 1, 0x80000004, None, None, dacl, None) != 0:
            raise W2gAclError("private directory DACL installation failed")
    finally:
        kernel.LocalFree(descriptor)


def protect_one(path: Path, *, directory: bool) -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components
    from serverfs_mcp.windows_core_control import _check_private
    from serverfs_mcp.windows_private_artifacts import protect_new_file

    _no_reparse_components(path)
    sid = current_user_sid()
    if directory:
        protect_private_directory(path, sid)
    else:
        protect_new_file(path, sid)
    _check_private(path, directory=directory, sid=sid)


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in ("dir", "file"):
        print("W2G_DACL_FAILED=BAD_ARGS", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "src"))
    try:
        protect_one(Path(sys.argv[1]), directory=sys.argv[2] == "dir")
    except Exception as exc:  # noqa: BLE001 - never emit SDDL or private paths
        print(f"W2G_DACL_FAILED={type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

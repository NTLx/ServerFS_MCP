"""Win32 protected-DACL staging for new private ServerFS service artifacts.

Creates no public/workdir files, grants only current user + SYSTEM +
Administrators. Mutations target a *new* temporary file in a separately
pre-provisioned, validated same-user service directory; never an existing
operator-owned credential or an already registered Windows task.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path


class PrivateArtifactError(Exception):
    """Fixed private-staging failure without SDDL or private path disclosure."""


def protect_new_file(path: Path, sid: str) -> None:
    """Set D:P current-user DACL on one newly created file.

    SetNamedSecurityInfoW opens the file for WRITE_DAC under the current
    owner's user token. The private parent directory must be checked by the
    caller before file creation and the result revalidated before publication.
    """
    if sys.platform != "win32" or os.name != "nt":
        raise PrivateArtifactError("protected artifact staging requires Windows")
    from .windows_cloudflare_security import _no_reparse_components

    try:
        _no_reparse_components(path)
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

        # No group/Everyone/AuthenticatedUsers ACE and no inherited ACE.
        sddl = f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)"
        descriptor = ctypes.c_void_p()
        if not convert(sddl, 1, ctypes.byref(descriptor), None):
            raise PrivateArtifactError("private artifact DACL could not be generated")
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
                raise PrivateArtifactError("private artifact DACL cannot be read")
            # SE_FILE_OBJECT, DACL_SECURITY_INFORMATION | PROTECTED_DACL.
            result = set_security(str(path), 1, 0x80000004, None, None, dacl, None)
            if result != 0:
                raise PrivateArtifactError("private artifact DACL could not be installed")
        finally:
            kernel.LocalFree(descriptor)
    except PrivateArtifactError:
        raise
    except Exception as exc:  # noqa: BLE001 - fixed and credential-free error
        raise PrivateArtifactError("private artifact Windows ACL validation failed") from exc

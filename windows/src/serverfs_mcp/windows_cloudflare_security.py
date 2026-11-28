"""Fail-closed Windows credential file boundary for optional Cloudflare ingress.

This module never creates, repairs or reads credential contents. Operators must
prepare a current-user-owned protected DACL before installing a connector.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SID_RE = re.compile(r"S-1-(?:[0-9]+-)+[0-9]+\Z")


# Use native Win32 security descriptors rather than a PowerShell subprocess.
# A pwsh-parent / Windows PowerShell 5.1 module-path mismatch broke Get-Acl
# autoload on GitHub Actions. Direct Advapi32 calls keep validation fail-closed
# without depending on shell state or exposing credential paths in argv.
def _read_windows_file_sddl(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    owner_and_dacl = 0x00000001 | 0x00000004
    descriptor = ctypes.c_void_p()
    owner_pointer = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        owner_and_dacl,
        ctypes.byref(owner_pointer),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result != 0 or not descriptor.value:
        raise WindowsPrivateFileError("Windows credential DACL verification unavailable")
    try:
        owner_text = ctypes.c_void_p()
        if (
            not owner_pointer.value
            or not advapi32.ConvertSidToStringSidW(owner_pointer, ctypes.byref(owner_text))
            or not owner_text.value
        ):
            raise WindowsPrivateFileError("Windows credential owner SID is unavailable")
        try:
            canonical_owner = ctypes.wstring_at(owner_text.value)
        finally:
            kernel32.LocalFree(owner_text)
        sddl = ctypes.c_void_p()
        if (
            not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, owner_and_dacl, ctypes.byref(sddl), None
            )
            or not sddl.value
        ):
            raise WindowsPrivateFileError("Windows credential DACL verification unavailable")
        try:
            return _canonicalize_windows_sddl(ctypes.wstring_at(sddl.value), canonical_owner)
        finally:
            kernel32.LocalFree(sddl)
    finally:
        kernel32.LocalFree(descriptor)


def _sddl_sections(sddl: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    markers: list[tuple[str, int]] = []
    depth = 0
    for index, char in enumerate(sddl):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise WindowsPrivateFileError("credential file owner or DACL is not private")
        if depth == 0 and index + 1 < len(sddl) and char in "OGDS" and sddl[index + 1] == ":":
            markers.append((char, index))
    if depth != 0 or not markers or markers[0][1] != 0:
        raise WindowsPrivateFileError("credential file owner or DACL is not private")
    for number, (label, pos) in enumerate(markers):
        if label in sections:
            raise WindowsPrivateFileError("credential file owner or DACL is not private")
        end = markers[number + 1][1] if number + 1 < len(markers) else len(sddl)
        sections[label] = sddl[pos + 2 : end]
    return sections


def _canonicalize_windows_sddl(sddl: str, owner_sid: str) -> str:
    """Normalize the native owner's numeric SID rather than guessing from aliases.

    Win32 may encode built-in local Administrator as LA. Normalize a LA ACE
    only when the actual owner is verified as local Administrator (RID 500).
    """
    original_owner = _sddl_sections(sddl).get("O")
    if not original_owner or not _SID_RE.fullmatch(owner_sid):
        raise WindowsPrivateFileError("credential file owner or DACL is not private")
    if original_owner == "LA":
        if not owner_sid.endswith("-500"):
            raise WindowsPrivateFileError("credential file owner or DACL is not private")
        sddl = sddl.replace(";;;LA)", f";;;{owner_sid})")
    return sddl.replace(f"O:{original_owner}", f"O:{owner_sid}", 1)


def _validate_private_sddl(sddl: str, user_sid: str) -> None:
    parts = _sddl_sections(sddl.strip())
    if parts.get("O") != user_sid:
        raise WindowsPrivateFileError("credential file owner or DACL is not private")
    dacl = parts.get("D", "")
    first_ace = dacl.find("(")
    flags = dacl if first_ace < 0 else dacl[:first_ace]
    if not re.fullmatch(r"(?:P|AI|AR)+", flags) or "P" not in flags:
        raise WindowsPrivateFileError("credential file owner or DACL is not private")
    ace_text = "" if first_ace < 0 else dacl[first_ace:]
    aces = re.findall(r"\([^()]*\)", ace_text)
    if not aces or "".join(aces) != ace_text:
        raise WindowsPrivateFileError("credential file owner or DACL is not private")
    allowed_sids = {user_sid, "SY", "BA", "OW", "S-1-5-18", "S-1-5-32-544", "S-1-3-4"}
    owner_grant = False
    for ace in aces:
        fields = ace[1:-1].split(";")
        if len(fields) != 6 or fields[0] not in {"A", "D"} or "ID" in fields[1]:
            raise WindowsPrivateFileError("credential file owner or DACL is not private")
        if fields[0] == "A":
            if fields[5] not in allowed_sids:
                raise WindowsPrivateFileError("credential file owner or DACL is not private")
            owner_grant |= fields[5] == user_sid
    if not owner_grant:
        raise WindowsPrivateFileError("credential file owner or DACL is not private")


class WindowsPrivateFileError(Exception):
    """Safe error without file contents, account names or user-supplied paths."""


def _within(path: Path, root: Path) -> bool:
    """Case-insensitive comparison on Windows, including the root itself."""
    normalized_path = os.path.normcase(os.path.normpath(str(path)))
    normalized_root = os.path.normcase(os.path.normpath(str(root)))
    try:
        return os.path.commonpath((normalized_path, normalized_root)) == normalized_root
    except ValueError:  # Different Windows volumes.
        return False


def _no_reparse_components(path: Path) -> None:
    current = path
    while True:
        try:
            details = current.lstat()
        except OSError as exc:
            raise WindowsPrivateFileError("credential path is unavailable") from exc
        if stat.S_ISLNK(details.st_mode) or (
            getattr(details, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise WindowsPrivateFileError("credential path contains a reparse point")
        if current.parent == current:
            break
        current = current.parent


def _is_windows() -> bool:
    return sys.platform == "win32" and os.name == "nt"


def validate_private_file(
    path: Path,
    *,
    checkout_root: Path,
    workdir_roots: Iterable[Path],
    user_sid: str,
    sddl_reader: Callable[[Path], str] = _read_windows_file_sddl,
) -> Path:
    """Validate existing token/env file; return its fixed resolved absolute path.

    Native Win32 reads the owner and DACL into SDDL without spawning a
    subprocess. ACL checks fail closed and never modify operator-owned files.
    """
    if not _is_windows():
        raise WindowsPrivateFileError("Windows credential validation requires Windows")
    if not _SID_RE.fullmatch(user_sid):
        raise WindowsPrivateFileError("current user SID is invalid")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise WindowsPrivateFileError("credential file path must be absolute")
    _no_reparse_components(candidate)
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise WindowsPrivateFileError("credential path is not a regular file")
        boundaries = (checkout_root, *workdir_roots)
        if any(_within(resolved, Path(root).resolve(strict=True)) for root in boundaries):
            raise WindowsPrivateFileError("credential path overlaps a ServerFS workdir")
    except WindowsPrivateFileError:
        raise
    except OSError as exc:
        raise WindowsPrivateFileError("credential path cannot be validated") from exc

    try:
        sddl = sddl_reader(resolved)
    except WindowsPrivateFileError:
        raise
    except (OSError, ValueError) as exc:
        raise WindowsPrivateFileError("Windows credential DACL verification unavailable") from exc
    _validate_private_sddl(sddl, user_sid)
    return resolved

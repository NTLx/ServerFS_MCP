"""Fail-closed Windows credential file boundary for optional Cloudflare ingress.

This module never creates, repairs or reads credential contents. Operators must
prepare a current-user-owned protected DACL before installing a connector.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SID_RE = re.compile(r"S-1-(?:[0-9]+-)+[0-9]+\Z")

# PowerShell Get-Acl.Sddl is available even in ConstrainedLanguage. Avoid
# invoking arbitrary .NET methods on ACL objects (blocked on locked-down hosts).
# Trust decisions are made in Python against the normalized SDDL SID identifiers.
_READ_SDDL_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$acl = Get-Acl -LiteralPath $env:SERVERFS_PRIVATE_FILE_PATH -ErrorAction Stop; "
    "Write-Output $acl.Sddl"
)


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
    allowed_sids = {user_sid, "SY", "BA", "S-1-5-18", "S-1-5-32-544"}
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
    runner: Callable = subprocess.run,
) -> Path:
    """Validate existing token/env file; return its fixed resolved absolute path.

    No credentials appear in argv: PowerShell receives only a filename via
    a subprocess-local environment. Python validates SDDL against the current SID;
    ACL checks are
    fail-closed and do not attempt to modify operator-owned files.
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

    environment = os.environ.copy()
    environment["SERVERFS_PRIVATE_FILE_PATH"] = str(resolved)
    try:
        result = runner(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _READ_SDDL_SCRIPT,
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WindowsPrivateFileError("Windows credential DACL verification unavailable") from exc
    if result.returncode != 0:
        raise WindowsPrivateFileError("credential file owner or DACL is not private")
    _validate_private_sddl(result.stdout.strip(), user_sid)
    return resolved

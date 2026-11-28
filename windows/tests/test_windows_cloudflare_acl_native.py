"""Real Windows NTFS DACL acceptance on disposable private-file fixtures.

No production secrets, service registration, or scheduled task are touched.
This test intentionally fails if current-user protected ACL guarantees cannot be
established using only normal user privileges on the Windows CI runner.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_mcp.windows_cloudflare_security import (
    WindowsPrivateFileError,
    validate_private_file,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows NTFS DACL only")


def _current_sid() -> str:
    """Obtain the user SID without invoking PowerShell .NET methods."""
    import csv

    completed = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, "could not obtain current Windows test SID"
    records = list(csv.reader(completed.stdout.splitlines()))
    assert records and len(records[0]) >= 2, "Windows user SID response is invalid"
    return records[0][1].strip()


def _icacls(token: Path, *arguments: str) -> None:
    """Change only this disposable fixture's DACL; never a parent directory."""
    completed = subprocess.run(
        ["icacls.exe", str(token), *arguments],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, "temporary Windows NTFS ACL fixture setup failed"


def test_windows_private_acl_accepts_owner_and_rejects_foreign_allow(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "fixture.token"
    secret_file.write_text("fixture-value-not-a-real-credential", encoding="utf-8")
    workdir = tmp_path / "readonly-fixture"
    workdir.mkdir()
    checkout = Path(__file__).resolve().parents[1]

    user_sid = _current_sid()
    _icacls(secret_file, "/grant:r", f"*{user_sid}:(F)")
    _icacls(secret_file, "/inheritance:r")
    _icacls(secret_file, "/setowner", f"*{user_sid}")
    assert (
        validate_private_file(
            secret_file,
            checkout_root=checkout,
            workdir_roots=[workdir],
            user_sid=user_sid,
        )
        == secret_file.resolve()
    )

    _icacls(secret_file, "/grant", "*S-1-1-0:(R)")
    with pytest.raises(WindowsPrivateFileError, match="owner or DACL"):
        validate_private_file(
            secret_file,
            checkout_root=checkout,
            workdir_roots=[workdir],
            user_sid=user_sid,
        )

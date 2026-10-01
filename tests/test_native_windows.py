r"""Real-import smoke for the Windows native extension module.

Runs the built ``serverfs_windows_native`` pyd (PYTHONPATH-provided in
development, installed wheel in CI/Phase E) against real NTFS. Skipped
everywhere the module or platform is absent.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native kernel")

n = pytest.importorskip(
    "serverfs_windows_native",
    reason="serverfs-windows-native wheel/pyd not installed",
)

REJECTED_ROOTS = [
    ("C:\\Projects\\", "trailing separator on a directory"),
    ("\\\\.\\PhysicalDrive0", "device namespace"),
    ("\\\\?\\GLOBALROOT\\Device", "global object namespace"),
    ("\\\\?\\UNC\\server\\share", "extended UNC"),
    ("\\\\server\\share", "UNC"),
    ("\\\\?\\Volume{60392b61-0000-0000-0000-100000000000}\\", "volume GUID namespace"),
    ("relative\\path", "relative path"),
    ("C:/forward", "forward slashes"),
    ("", "empty path"),
]


def test_open_workdir_retains_identity(tmp_path: Path) -> None:
    session = n.open_workdir(str(tmp_path), True)
    first = session.object_token()
    assert first.startswith("vol-") and ":file-" in first
    # retention: token comes from the held handle, re-read equals itself
    assert session.object_token() == first
    assert session.read_only is True


@pytest.mark.parametrize(("root", "why"), REJECTED_ROOTS)
def test_open_workdir_refuses_out_of_scope_roots(root: str, why: str) -> None:
    with pytest.raises(n.NativeSessionError) as excinfo:
        n.open_workdir(root, True)
    assert excinfo.value.args[0] == "INVALID_ROOT", why


def test_missing_root_fails_not_found() -> None:
    missing = Path(tempfile.gettempdir()) / "serverfs_native_absent_dir_xyz"
    with pytest.raises(n.NativeSessionError) as excinfo:
        n.open_workdir(str(missing), True)
    assert excinfo.value.args[0] == "PATH_NOT_FOUND"


def test_error_messages_carry_no_host_paths(tmp_path: Path) -> None:
    with pytest.raises(n.NativeSessionError) as excinfo:
        n.open_workdir(str(tmp_path) + "\\", True)
    message = str(excinfo.value)
    assert str(tmp_path) not in message

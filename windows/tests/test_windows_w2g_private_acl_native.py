"""Native user-level directory DACL probe before W2g private venv staging.

The test touches only its own pytest temporary files. It never modifies a
ServerFS service directory, Scheduler entry, provider or production credential.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_cloudflare_security import (
    _read_windows_file_sddl,
    _validate_private_sddl,
)
from serverfs_mcp.windows_core_control import _check_private
from serverfs_mcp.windows_private_artifacts import protect_new_file

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native ACL only")


def _installer_module():
    script = Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2g-private-acl.py"
    spec = importlib.util.spec_from_file_location("w2g_private_acl_probe", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_protected_directory_allows_private_child_install(tmp_path: Path) -> None:
    target = tmp_path / "w2g-owned-env"
    sibling = tmp_path / "unrelated-file.txt"
    sibling.write_text("unchanged", encoding="utf-8")
    target.mkdir()
    sid = current_user_sid()

    _installer_module().protect_private_directory(target, sid)
    _check_private(target, directory=True, sid=sid)

    # A non-inheritable directory ACL would make these uv-style writes fail.
    scripts = target / "Scripts"
    scripts.mkdir()
    pyw = scripts / "pythonw.exe"
    pyw.write_bytes(b"disposable test-only file")
    assert pyw.read_bytes() == b"disposable test-only file"

    # Native protected file DACL remains compatible with the directory DACL.
    protect_new_file(pyw, sid)
    _validate_private_sddl(_read_windows_file_sddl(pyw), sid)
    assert sibling.read_text(encoding="utf-8") == "unchanged"


def test_non_directory_is_rejected_without_altering_bytes(tmp_path: Path) -> None:
    artifact = tmp_path / "not-a-directory.txt"
    artifact.write_bytes(b"untouched")
    with pytest.raises(Exception, match="not a directory"):
        _installer_module().protect_private_directory(artifact, current_user_sid())
    assert artifact.read_bytes() == b"untouched"

"""Native protected-DACL acceptance in an isolated temporary directory.

Only a synthetic test file gets a new DACL. No production credentials,
ServerFS service directory, Task Scheduler entry, or Agent state is touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_cloudflare_security import (
    _read_windows_file_sddl,
    _validate_private_sddl,
)
from serverfs_mcp.windows_private_artifacts import protect_new_file

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows kernel only")


def test_new_private_artifact_has_real_protected_user_dacl(tmp_path: Path) -> None:
    artifact = tmp_path / "new-core-state.tmp"
    artifact.write_bytes(b"synthetic test only")
    sid = current_user_sid()
    protect_new_file(artifact, sid)
    _validate_private_sddl(_read_windows_file_sddl(artifact), sid)
    assert artifact.read_bytes() == b"synthetic test only"


def test_dacl_staging_does_not_touch_sibling_file(tmp_path: Path) -> None:
    artifact = tmp_path / "private-manifest.tmp"
    sibling = tmp_path / "unrelated-sibling.txt"
    artifact.write_bytes(b"manifest fixture")
    sibling.write_bytes(b"unchanged")
    sid = current_user_sid()
    protect_new_file(artifact, sid)
    _validate_private_sddl(_read_windows_file_sddl(artifact), sid)
    assert sibling.read_bytes() == b"unchanged"

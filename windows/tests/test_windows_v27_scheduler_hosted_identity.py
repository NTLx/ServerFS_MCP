"""Verify the known hosted built-in Administrator ACL refusal, not rollback."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_cloudflare_security import _read_windows_file_sddl, _sddl_sections
from serverfs_mcp.windows_private_artifacts import protect_new_file
from serverfs_mcp.windows_v25 import storage

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("SERVERFS_WINDOWS_SCHEDULER_ROLLBACK") != "1",
    reason="explicit hosted Windows account compatibility check required",
)


def test_hosted_builtin_admin_refuses_private_owner(tmp_path: Path) -> None:
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    sid = current_user_sid()
    assert sid.endswith("-500"), "unexpected account; full rollback required"
    path = tmp_path / "private"
    path.mkdir()
    protect_new_file(path, sid)
    sections = _sddl_sections(_read_windows_file_sddl(path).strip())
    # This is the exact owner mismatch observed in the original hosted run.
    assert sections.get("O") in {"BA", "S-1-5-32-544"}
    assert sections["O"] != sid
    with pytest.raises(storage.StorageError, match="owner/DACL is unverified"):
        storage.check_private(path, directory=True, sid=sid)
    print("HOSTED_BUILTIN_ADMIN_ACL_REFUSAL_PASS; REAL_ROLLBACK_NOT_EXECUTED")

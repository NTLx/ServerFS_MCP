"""Opt-in W2 Windows private atomic artifact publication (temporary files only).

This tests os.link/ACL persistence on NTFS with a real current-user token.
It never writes to a ServerFS service home or changes production credentials.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_cloudflare_security import (
    _read_windows_file_sddl,
    _validate_private_sddl,
)
from serverfs_mcp.windows_core_control import CoreControlError, _atomic_create

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows NTFS only"),
    pytest.mark.skipif(
        os.environ.get("SERVERFS_W2_PRIVATE_PUBLISH") != "1",
        reason="explicit native private staging test opt-in required",
    ),
]


def test_private_hardlink_publish_preserves_acl_and_bytes(tmp_path: Path) -> None:
    target = tmp_path / "new-service-manifest.json"
    _atomic_create(target, b'{"synthetic":true}')
    assert target.read_bytes() == b'{"synthetic":true}'
    _validate_private_sddl(_read_windows_file_sddl(target), current_user_sid())
    assert not tuple(tmp_path.glob(".*.tmp-*"))


def test_private_publish_refuses_overwrite_without_mutation(tmp_path: Path) -> None:
    target = tmp_path / "new-service-state.json"
    _atomic_create(target, b"original")
    with pytest.raises(CoreControlError, match="artifact staging failed"):
        _atomic_create(target, b"untrusted overwrite")
    assert target.read_bytes() == b"original"
    _validate_private_sddl(_read_windows_file_sddl(target), current_user_sid())
    assert not tuple(tmp_path.glob(".*.tmp-*"))

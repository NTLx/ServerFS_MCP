"""Opt-in native Windows Bridge private-artifact staging, no Bridge process.

Every operation targets only a disposable pytest tmp_path protected directory.
No Task Scheduler, lifecycle lease, named Job, Provider or exposed workdir use.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from serverfs_mcp import windows_bridge_control as control
from serverfs_mcp import windows_bridge_service as service
from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_cloudflare_security import (
    _read_windows_file_sddl,
    _validate_private_sddl,
)
from serverfs_mcp.windows_private_artifacts import protect_new_file

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL only"),
    pytest.mark.skipif(
        os.environ.get("SERVERFS_W2_BRIDGE_PRIVATE") != "1",
        reason="explicit private Bridge fixture opt-in required",
    ),
]


@pytest.fixture
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "bridge-owner"
    home.mkdir()
    protect_new_file(home, current_user_sid())
    monkeypatch.setattr(service, "service_home", lambda: home)
    return home


def _check_file(path: Path) -> None:
    _validate_private_sddl(_read_windows_file_sddl(path), current_user_sid())


def test_bridge_create_only_artifact_is_private(private_home: Path) -> None:
    path = private_home / "task.xml"
    control._atomic_write(path, b"synthetic source XML")
    assert path.read_bytes() == b"synthetic source XML"
    _check_file(path)
    assert not tuple(private_home.glob(".*.tmp-*"))


def test_bridge_private_artifact_refuses_overwrite(private_home: Path) -> None:
    path = private_home / "service.json"
    control._atomic_write(path, b"original")
    with pytest.raises(control.BridgeControlError, match="could not be written"):
        control._atomic_write(path, b"untrusted replacement")
    assert path.read_bytes() == b"original"
    _check_file(path)
    assert not tuple(private_home.glob(".*.tmp-*"))


def test_watchdog_state_update_preserves_private_acl(private_home: Path) -> None:
    path = service.watchdog_state_path()
    service._atomic_write_state(path, {"component": "agent-bridge", "stage": "owner-started"})
    _check_file(path)
    assert service._read_state(path)["stage"] == "owner-started"
    service._atomic_write_state(
        path,
        {"component": "agent-bridge", "stage": "owner-started", "runner_restart_count": 2},
    )
    _check_file(path)
    assert json.loads(path.read_text(encoding="utf-8"))["runner_restart_count"] == 2
    assert not tuple(private_home.glob(".*.tmp-*"))

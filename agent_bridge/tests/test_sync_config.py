"""macOS/native private Bridge policy synchronization regressions."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.sync_config import (
    check_bridge_config,
    configure_bridge_config,
    sync_bridge_config,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX private-state contract")


def _write_private(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _base_config(tmp_path: Path, workdir: Path, *, authenticated_proxy: bool = False) -> dict:
    state = tmp_path / "state"
    locks = tmp_path / "locks"
    codex_home = tmp_path / "codex-home"
    state.mkdir(mode=0o700)
    locks.mkdir(mode=0o700)
    codex_home.mkdir(exist_ok=True)
    proxy_url = (
        "http://user:password@127.0.0.1:7897" if authenticated_proxy else "http://127.0.0.1:7897"
    )
    return {
        "lease_key": "alias",
        "socket_path": str(tmp_path / "bridge.sock"),
        "state_dir": str(state),
        "lock_dir": str(locks),
        "allowed_peer_uid": os.getuid(),
        "allowed_peer_gid": os.getgid(),
        "enable_fake_runtime": False,
        "limits": {
            "task_timeout_seconds": 7200,
            "interaction_timeout_seconds": 1800,
            "max_active_tasks": 4,
            "retention_seconds": 604800,
        },
        "codex": {
            "enabled": True,
            "codex_bin": "codex",
            "codex_home": str(codex_home),
            "use_proxy": False,
        },
        "proxy": {"url": proxy_url, "authenticated": authenticated_proxy},
        "jev": {"api_key": "jev-private-marker", "use_proxy": True},
        "workdirs": [
            {
                "alias": "old",
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": ["codex"],
            }
        ],
    }


def _request(workdir: Path, *, use_proxy: bool = True) -> dict:
    return {
        "workdirs": [
            {
                "alias": "serverfs",
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": ["codex"],
            }
        ],
        "runtimes": {
            "codex": {
                "enabled": True,
                "codex_bin": "codex",
                "use_proxy": use_proxy,
            }
        },
        "limits": {
            "task_timeout_seconds": 7200,
            "interaction_timeout_seconds": 1800,
            "max_active_tasks": 4,
            "retention_seconds": 604800,
        },
    }


def test_sync_replaces_only_derived_policy_and_preserves_private_material(tmp_path: Path) -> None:
    old_workdir = tmp_path / "old-workdir"
    new_workdir = tmp_path / "serverfs"
    old_workdir.mkdir()
    new_workdir.mkdir()
    config_path = tmp_path / "bridge.json"
    original = _base_config(tmp_path, old_workdir)
    _write_private(config_path, original)

    request = _request(new_workdir)
    assert check_bridge_config(config_path, request) is False

    sync_bridge_config(config_path, request)

    assert check_bridge_config(config_path, request) is True
    synced = json.loads(config_path.read_text(encoding="utf-8"))
    assert synced["workdirs"][0]["alias"] == "serverfs"
    assert synced["workdirs"][0]["host_path"] == str(new_workdir)
    assert synced["proxy"] == original["proxy"]
    assert synced["jev"] == original["jev"]
    assert synced["socket_path"] == original["socket_path"]
    assert synced["allowed_peer_uid"] == original["allowed_peer_uid"]
    assert synced["codex"]["codex_home"] == original["codex"]["codex_home"]
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_configure_can_create_private_config_from_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    config_path = tmp_path / "nested" / "agent-bridge" / "bridge.json"
    private = {
        "proxy": {"url": "http://127.0.0.1:7897", "authenticated": False},
        "jev": {"api_key": "jev-private-marker", "use_proxy": True},
    }

    configure_bridge_config(config_path, _request(workdir), private)

    configured = json.loads(config_path.read_text(encoding="utf-8"))
    assert configured["workdirs"][0]["alias"] == "serverfs"
    assert configured["proxy"] == private["proxy"]
    assert configured["jev"] == private["jev"]
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700


def test_configure_replaces_private_overlay_without_preserving_removed_secret(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    config_path = tmp_path / "bridge.json"
    original = _base_config(tmp_path, workdir)
    _write_private(config_path, original)

    configure_bridge_config(
        config_path,
        _request(workdir, use_proxy=False),
        {"jev": {"api_key": None, "use_proxy": False}},
    )

    configured = json.loads(config_path.read_text(encoding="utf-8"))
    assert "proxy" not in configured
    assert configured["jev"] == {"api_key": None, "use_proxy": False}
    assert configured["socket_path"] == original["socket_path"]
    assert configured["codex"]["codex_home"] == original["codex"]["codex_home"]


def test_invalid_merged_policy_is_refused_without_replacing_live_config(tmp_path: Path) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    config_path = tmp_path / "bridge.json"
    original = _base_config(tmp_path, workdir, authenticated_proxy=True)
    _write_private(config_path, original)
    before = config_path.read_bytes()

    with pytest.raises(BridgeError, match="synchronized Bridge config is invalid"):
        sync_bridge_config(config_path, _request(workdir, use_proxy=True))

    assert config_path.read_bytes() == before

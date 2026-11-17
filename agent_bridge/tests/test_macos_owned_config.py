"""Source-only macOS v0.25 owned Bridge private config staging regressions."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.macos_owned_config import (
    check_named_owned_config,
    named_socket_path,
    stage_named_owned_config,
)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="native macOS only")


def _home(tmp_path: Path, identifier: str = "named-stage") -> Path:
    base = tmp_path / "Library/Application Support/ServerFS/instances"
    app = base / identifier / "service-manager" / "data"
    app.mkdir(parents=True, mode=0o700)
    for parent in (base, base / identifier, app.parent, app):
        parent.chmod(0o700)
    return app / "agent-bridge"


def _request(workdir: Path, *, fake: bool = True) -> dict:
    return {
        "enable_fake_runtime": fake,
        "workdirs": [
            {
                "alias": "repo",
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "review",
                "agent_runtimes": ["fake"] if fake else [],
            }
        ],
    }


def test_owned_bridge_renders_valid_private_new_config(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    home = _home(tmp_path)
    result = stage_named_owned_config("named-stage", home, _request(workdir), allow_test_fake=True)
    assert result == {
        "instance_id": "named-stage",
        "state": "staged-not-registered",
        "private_config": "verified",
        "workdirs": 1,
    }
    config_path = home / "bridge.json"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert all(
        stat.S_IMODE(folder.stat().st_mode) == 0o700
        for folder in (home, home / "state", home / "locks")
    )
    actual = BridgeConfig.load(config_path)
    assert actual.socket_path == named_socket_path("named-stage")
    assert actual.allowed_peer_uid == os.getuid()
    assert actual.allowed_peer_gid is None
    assert actual.state_dir == home / "state"
    assert actual.lock_dir == home / "locks"
    assert actual.enable_fake_runtime is True
    assert actual.policies.get("repo").host_path == workdir
    assert not actual.socket_path.exists()
    assert not actual.socket_path.parent.exists()
    assert "jev" not in json.loads(config_path.read_text())
    assert "proxy" not in json.loads(config_path.read_text())


def test_owned_bridge_refuses_fake_in_production_mode(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    home = _home(tmp_path)
    with pytest.raises(BridgeError, match="fake runtime"):
        stage_named_owned_config("named-stage", home, _request(workdir))
    assert not home.exists()


def test_owned_bridge_never_overwrites_existing_private_config(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    home = _home(tmp_path)
    stage_named_owned_config("named-stage", home, _request(workdir), allow_test_fake=True)
    previous = (home / "bridge.json").read_bytes()
    with pytest.raises(BridgeError, match="already exists"):
        stage_named_owned_config("named-stage", home, _request(workdir), allow_test_fake=True)
    assert (home / "bridge.json").read_bytes() == previous


def test_owned_bridge_refuses_wrong_owner_and_private_mode(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    home = _home(tmp_path)
    with pytest.raises(BridgeError, match="instance ID disagrees"):
        stage_named_owned_config("another", home, _request(root), allow_test_fake=True)
    parent = home.parent
    parent.chmod(0o755)
    with pytest.raises(BridgeError, match="not private"):
        stage_named_owned_config("named-stage", home, _request(root), allow_test_fake=True)
    assert not home.exists()


def test_owned_bridge_rejects_unowned_or_unsafe_socket_namespace(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    home = _home(tmp_path)
    occupied = named_socket_path("named-stage").parent
    if occupied.exists():
        pytest.skip("named test socket namespace already belongs to another process")
    try:
        occupied.mkdir(mode=0o700)
        with pytest.raises(BridgeError, match="occupied"):
            stage_named_owned_config("named-stage", home, _request(root), allow_test_fake=True)
        assert not home.exists()
    finally:
        occupied.rmdir()


def test_owned_bridge_private_config_rejects_invalid_workdirs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    home = _home(tmp_path)
    invalid = _request(root)
    invalid["workdirs"][0]["read_only"] = "false"
    with pytest.raises(BridgeError):
        stage_named_owned_config("named-stage", home, invalid, allow_test_fake=True)
    assert not home.exists()


def test_owned_bridge_check_proves_policy_sync_and_no_mutation(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    home = _home(tmp_path)
    policy = _request(workdir)
    stage_named_owned_config("named-stage", home, policy, allow_test_fake=True)
    path = home / "bridge.json"
    before = path.read_bytes()
    result = check_named_owned_config("named-stage", home, policy, allow_test_fake=True)
    assert result == {
        "instance_id": "named-stage",
        "state": "verified-not-registered",
        "policy": "synced",
        "workdirs": 1,
    }
    assert path.read_bytes() == before


def test_owned_bridge_check_rejects_policy_drift_without_rewriting(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    home = _home(tmp_path)
    policy = _request(workdir)
    stage_named_owned_config("named-stage", home, policy, allow_test_fake=True)
    path = home / "bridge.json"
    before = path.read_bytes()
    changed = _request(workdir)
    changed["workdirs"][0]["read_only"] = True
    with pytest.raises(BridgeError, match="policy drift"):
        check_named_owned_config("named-stage", home, changed, allow_test_fake=True)
    assert path.read_bytes() == before


def test_owned_bridge_check_refuses_added_gid_shared_socket_mode(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    home = _home(tmp_path)
    policy = _request(workdir)
    stage_named_owned_config("named-stage", home, policy, allow_test_fake=True)
    path = home / "bridge.json"
    document = json.loads(path.read_text())
    document["allowed_peer_gid"] = os.getgid()
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    with pytest.raises(BridgeError, match="identity drift"):
        check_named_owned_config("named-stage", home, policy, allow_test_fake=True)


def test_owned_bridge_cannot_expose_private_state_as_workdir(tmp_path: Path) -> None:
    home = _home(tmp_path)
    policy = _request(tmp_path)
    policy["workdirs"][0]["host_path"] = str(home.parent.parent)
    with pytest.raises(BridgeError, match="private service state"):
        stage_named_owned_config("named-stage", home, policy, allow_test_fake=True)
    assert not home.exists()

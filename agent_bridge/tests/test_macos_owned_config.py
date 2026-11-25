"""Source-only macOS v0.25 owned Bridge private config staging regressions."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.macos_owned_config import (
    check_named_owned_config,
    named_socket_path,
    stage_named_owned_config,
    stage_named_owned_v2_config,
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
        "protocol_mode": "v1",
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


def test_owned_v2_staging_persists_self_only_agent_grants(tmp_path: Path, monkeypatch) -> None:
    from serverfs_agent_bridge import core_grants
    from serverfs_agent_bridge.core_grants import sign_core_request
    from serverfs_agent_bridge.core_grants_private import load_private_core_grants
    from serverfs_agent_bridge.store import TaskStore

    workdir = tmp_path / "repo"
    workdir.mkdir()
    disabled = tmp_path / "files"
    disabled.mkdir()
    second_workdir = tmp_path / "notes"
    second_workdir.mkdir()
    home = _home(tmp_path, "owned-v2")
    socket = tmp_path / "runtime" / "bridge.sock"
    monkeypatch.setattr(
        "serverfs_agent_bridge.macos_owned_config.named_socket_path", lambda _: socket
    )
    monkeypatch.setattr(
        core_grants, "_WORKDIR_METHODS", core_grants._WORKDIR_METHODS | {"task.list"}
    )
    request = {
        "enable_fake_runtime": True,
        "workdirs": [
            {
                "alias": "repo",
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "review",
                "agent_runtimes": ["fake"],
            },
            {
                "alias": "files",
                "host_path": str(disabled),
                "read_only": True,
                "agent_mode": "disabled",
                "agent_runtimes": [],
            },
            {
                "alias": "notes",
                "host_path": str(second_workdir),
                "read_only": False,
                "agent_mode": "review",
                "agent_runtimes": ["fake"],
            },
        ],
    }
    material = [
        {"alias": "repo", "key_id": "owned-a", "secret_hex": "ab" * 32},
        {"alias": "notes", "key_id": "owned-b", "secret_hex": "cd" * 32},
    ]

    result = stage_named_owned_v2_config("owned-v2", home, request, material, allow_test_fake=True)

    config = BridgeConfig.load(home / "bridge.json")
    assert config.protocol_mode == "v2"
    assert config.bridge_id == "owned-v2"
    assert check_named_owned_config("owned-v2", home, request, allow_test_fake=True) == {
        "instance_id": "owned-v2",
        "state": "verified-not-registered",
        "policy": "synced",
        "protocol_mode": "v2",
        "workdirs": 3,
    }
    scopes = {scope["alias"]: scope for scope in result["grant_scopes"]}
    assert set(scopes) == {"repo", "notes"}
    grants_path = home / "state" / "core-grants.json"
    assert stat.S_IMODE(grants_path.stat().st_mode) == 0o600
    document = json.loads(grants_path.read_text())
    assert document["bridge_id"] == "owned-v2"
    assert len(document["grants"]) == 2
    for grant in document["grants"]:
        assert grant["core_id"] == "owned-v2"
        assert len(grant["workdirs"]) == 1
        alias = grant["workdirs"][0]["alias"]
        assert grant["key_id"] == {"repo": "owned-a", "notes": "owned-b"}[alias]
        assert grant["secret_hex"] == {"repo": "ab" * 32, "notes": "cd" * 32}[alias]
        assert "task.list" in grant["methods"]
        assert not any(method.startswith("repo.") for method in grant["methods"])
    assert {item["workdirs"][0]["alias"] for item in document["grants"]} == {
        "repo",
        "notes",
    }
    loaded = load_private_core_grants(
        store=TaskStore(config.state_dir), bridge_id="owned-v2", policies=config.policies
    )
    repo_unsigned = {
        "protocol_version": 2,
        "bridge_id": "owned-v2",
        "core_id": "owned-v2",
        "key_id": "owned-a",
        "request_id": "req-test",
        "method": "task.list",
        "params": {"workdir": "repo"},
        "policy_revision": scopes["repo"]["policy_revision"],
        "issued_at": int(time.time()),
        "nonce": "1" * 32,
    }
    envelope = {
        **repo_unsigned,
        "signature": sign_core_request(repo_unsigned, bytes.fromhex("ab" * 32)),
    }
    assert loaded.authorize(envelope).workdir == "repo"
    notes_unsigned = {
        **repo_unsigned,
        "request_id": "req-cross-alias",
        "params": {"workdir": "notes"},
        "nonce": "2" * 32,
    }
    with pytest.raises(BridgeError, match="not authorized"):
        loaded.authorize(
            {
                **notes_unsigned,
                "signature": sign_core_request(notes_unsigned, bytes.fromhex("ab" * 32)),
            }
        )
    assert "ab" * 32 not in json.dumps(result)


def test_owned_v2_grants_separate_agent_repo_and_file_policies(tmp_path, monkeypatch):
    home = _home(tmp_path, "owned-v2")
    monkeypatch.setattr(
        "serverfs_agent_bridge.macos_owned_config.named_socket_path",
        lambda _: tmp_path / "runtime" / "bridge.sock",
    )
    workdirs = []
    materials = []
    for index, (alias, agent, repo, read_only) in enumerate(
        [
            ("Both", True, True, False),
            ("Agent", True, False, False),
            ("Repo", False, True, False),
            ("ReadRepo", False, True, True),
            ("Files", False, False, True),
        ]
    ):
        root = tmp_path / alias
        root.mkdir()
        workdirs.append(
            {
                "alias": alias,
                "host_path": str(root),
                "read_only": read_only,
                "agent_mode": "review" if agent else "disabled",
                "agent_runtimes": ["fake"] if agent else [],
                "repo_ops_enabled": repo,
            }
        )
        if agent or repo:
            materials.append(
                {"alias": alias, "key_id": f"key-{index}", "secret_hex": f"{index:02x}" * 32}
            )
    stage_named_owned_v2_config(
        "owned-v2",
        home,
        {"enable_fake_runtime": True, "workdirs": workdirs},
        materials,
        allow_test_fake=True,
    )
    document = json.loads((home / "state" / "core-grants.json").read_text())
    grants = {g["workdirs"][0]["alias"]: set(g["methods"]) for g in document["grants"]}
    repo_methods = {"repo.inspect", "repo.action", "repo.commit", "repo.push"}
    assert set(grants) == {"Both", "Agent", "Repo", "ReadRepo"}
    assert repo_methods <= grants["Both"]
    assert "task.list" in grants["Both"]
    assert "task.list" in grants["Agent"]
    assert not grants["Agent"] & repo_methods
    assert grants["Repo"] == {"bridge.ping"} | repo_methods
    assert grants["ReadRepo"] == {"bridge.ping", "repo.inspect"}


def test_owned_v2_staging_refuses_missing_public_task_list_before_writes(
    tmp_path: Path, monkeypatch
) -> None:
    from serverfs_agent_bridge import core_grants

    workdir = tmp_path / "repo"
    workdir.mkdir()
    home = _home(tmp_path, "owned-v2")
    socket = tmp_path / "runtime" / "bridge.sock"
    monkeypatch.setattr(
        "serverfs_agent_bridge.macos_owned_config.named_socket_path", lambda _: socket
    )
    methods = core_grants._WORKDIR_METHODS - {"task.list"}
    monkeypatch.setattr(core_grants, "_WORKDIR_METHODS", methods)
    with pytest.raises(BridgeError, match="task.list capability is unavailable"):
        stage_named_owned_v2_config(
            "owned-v2",
            home,
            _request(workdir),
            [{"alias": "repo", "key_id": "owned-key", "secret_hex": "cd" * 32}],
            allow_test_fake=True,
        )
    assert not (home / "bridge.json").exists()
    assert not (home / "state" / "core-grants.json").exists()


def test_owned_v2_grant_staging_never_overwrites_legacy_config(tmp_path: Path, monkeypatch) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    home = _home(tmp_path, "owned-v2")
    socket = tmp_path / "runtime" / "bridge.sock"
    monkeypatch.setattr(
        "serverfs_agent_bridge.macos_owned_config.named_socket_path", lambda _: socket
    )
    stage_named_owned_config("owned-v2", home, _request(workdir), allow_test_fake=True)
    path = home / "bridge.json"
    previous = path.read_bytes()
    with pytest.raises(BridgeError, match="already exists"):
        stage_named_owned_v2_config(
            "owned-v2",
            home,
            _request(workdir),
            [{"alias": "repo", "key_id": "owned-key", "secret_hex": "cd" * 32}],
            allow_test_fake=True,
        )
    assert path.read_bytes() == previous

"""Opt-in Windows Core v2-owned/shared Bridge binding and private capability tests.

Pure manifest, signed client construction and fake Job validators only; no
Named Pipe, Windows Job Object, Scheduler or installed private state touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.windows_v25 import core_bridge_binding as binding_module
from serverfs_mcp.windows_v25.bridge_peer_job import (
    BridgePeerJobError,
    make_bridge_peer_validator,
)
from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources
from serverfs_mcp.windows_v25.core_bridge_binding import (
    CoreBridgeBindingError,
    load_native_core_bridge_binding,
    parse_core_bridge_binding,
)
from serverfs_mcp.windows_v25.spec import Manifest

SID = "S-1-5-21-111-222-333-1001"


def _fixtures(tmp_path: Path, *, core_id="core-a", bridge_id="bridge-shared", mode="shared"):
    core = Manifest.parse(
        {
            "schema": 2,
            "component": "core",
            "instance_id": core_id,
            "sid": SID,
            "pythonw": str(tmp_path / "envs" / "core" / "Scripts" / "pythonw.exe"),
            "settings": {
                "config": str(tmp_path / "serverfs.toml"),
                "auth_file": str(tmp_path / "core.env"),
                "port": 18865,
                "hostname": "core.example.test",
            },
        }
    )
    bridge = Manifest.parse(
        {
            "schema": 2,
            "component": "bridge",
            "instance_id": bridge_id,
            "sid": SID,
            "pythonw": str(tmp_path / "envs" / "bridge" / "Scripts" / "pythonw.exe"),
            "settings": {
                "config": str(tmp_path / "bridge.json"),
                "bridge_python": str(tmp_path / "python.exe"),
            },
        }
    )
    doc = {
        "schema_version": 1,
        "instance_id": core_id,
        "bridge_mode": mode,
        "bridge_id": bridge_id,
        "key_id": "key-a",
        "secret_hex": "ab" * 32,
        "workdirs": [{"alias": "Repo", "policy_sha256": "a" * 64}],
    }
    return core, bridge, doc


def _parse(core, bridge, document):
    return parse_core_bridge_binding(
        core,
        document,
        local_aliases=frozenset({"Repo"}),
        verified_bridge=bridge,
        bridge_lock_dir=Path(r"C:\private\bridge-locks"),
    )


@pytest.mark.skipif(
    sys.platform != "win32", reason="requires native Windows Named Pipe path semantics"
)
def test_shared_core_binding_uses_verified_bridge_id_and_job(tmp_path: Path):
    core, bridge, document = _fixtures(tmp_path)
    binding = _parse(core, bridge, document)
    resources = WindowsBridgeResources(SID, "bridge-shared")
    assert binding.bridge_id == "bridge-shared"
    assert binding.bridge_mode == "shared"
    assert binding.pipe_name == resources.pipe_name
    assert binding.job_name == resources.job_name
    assert binding.lock_dir == Path(r"C:\private\bridge-locks")
    assert ("Repo", "a" * 64) in binding.policy_revisions
    assert "ab" * 32 not in repr(binding)
    settings, client = binding.build_wiring(
        log_level="WARNING", peer_validator=lambda pid: pid == 17
    )
    assert settings.agent_bridge_enabled
    assert settings.agent_bridge_socket == resources.pipe_name
    assert settings.agent_lock_dir == str(binding.lock_dir)
    assert settings.log_level == "WARNING"
    assert client.core_id == "core-a"
    assert client.bridge_id == "bridge-shared"
    assert client._server_pid_validator(17)
    assert not client._server_pid_validator(18)


def test_owned_core_binding_requires_matching_bridge_identity(tmp_path: Path):
    core, bridge, doc = _fixtures(tmp_path, core_id="core-a", bridge_id="core-a", mode="owned")
    assert _parse(core, bridge, doc).bridge_mode == "owned"


@pytest.mark.parametrize(
    "modify",
    [
        lambda d: d.update(instance_id="core-other"),
        lambda d: d.update(bridge_id="bridge-other"),
        lambda d: d.update(bridge_id="default"),
        lambda d: d.update(bridge_mode="disabled"),
        lambda d: d.update(bridge_mode="owned"),
        lambda d: d.update(key_id="../oops"),
        lambda d: d.update(secret_hex="weak"),
        lambda d: d.update(secret_hex="GG" * 32),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(extra="foreign"),
        lambda d: d.update(workdirs=[]),
        lambda d: d.update(workdirs=[{"alias": "Foreign", "policy_sha256": "a" * 64}]),
        lambda d: d.update(workdirs=[{"alias": "Repo", "policy_sha256": "INVALID"}]),
        lambda d: d.update(workdirs=[{"alias": "Repo", "policy_sha256": "a" * 64}] * 2),
    ],
)
def test_invalid_core_bindings_denied_without_key_material(tmp_path: Path, modify):
    core, bridge, document = _fixtures(tmp_path)
    modify(document)
    with pytest.raises(CoreBridgeBindingError) as denied:
        _parse(core, bridge, document)
    assert "ab" * 32 not in str(denied.value)
    assert "invalid or not private" in str(denied.value)


def test_untrusted_bridge_manifest_cannot_borrow_different_user_or_instance(tmp_path):
    core, bridge, document = _fixtures(tmp_path)
    altered = bridge.to_dict()
    altered["sid"] = "S-1-5-21-999-222-333-1001"
    with pytest.raises(CoreBridgeBindingError):
        _parse(core, Manifest.parse(altered), document)


def test_owner_private_core_binding_loader_reads_only_its_own_file(tmp_path, monkeypatch):
    core, bridge, document = _fixtures(tmp_path)
    core_home = tmp_path / "private-core"
    core_home.mkdir()
    bridge_home = tmp_path / "private-bridge"
    (bridge_home / "locks").mkdir(parents=True)
    path = core_home / "bridge-binding.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    bridge_config = bridge_home / "bridge.json"
    resources = WindowsBridgeResources(SID, bridge.instance_id)
    bridge_config.write_text(
        json.dumps(
            {
                "bridge_id": bridge.instance_id,
                "core_grants_enabled": True,
                "socket_path": resources.pipe_name,
                "state_dir": str(bridge_home / "state"),
                "lock_dir": str(bridge_home / "locks"),
                "allowed_peer_sid": SID,
                "workdirs": [
                    {
                        "alias": "Repo",
                        "host_path": str(tmp_path),
                        "read_only": False,
                        "agent_mode": "workspace-write",
                        "agent_runtimes": [],
                        "repo_ops_enabled": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    from serverfs_mcp.windows_v25 import bridge_v2_owner

    monkeypatch.setattr(
        bridge_v2_owner,
        "load_native_bridge_plan",
        lambda _manifest: pytest.fail("Core must never load Bridge owner health credentials"),
    )
    inspected = []

    def private(path, *, directory, sid):
        inspected.append((path, directory, sid))
        assert path.exists()
        assert sid == SID

    monkeypatch.setattr(
        binding_module.storage,
        "home",
        lambda component, instance: core_home if component == "core" else bridge_home,
    )
    monkeypatch.setattr(binding_module.storage, "load", lambda component, instance: bridge)
    monkeypatch.setattr(binding_module.storage, "check_private", private)
    workdirs = [
        SimpleNamespace(
            alias="Repo",
            root=tmp_path,
            read_only=False,
            agent_mode="workspace-write",
            agent_runtimes=frozenset(),
            policy=SimpleNamespace(repo_ops_enabled=False),
        )
    ]
    binding = load_native_core_bridge_binding(core, workdirs)
    assert binding.bridge_id == "bridge-shared"
    assert binding.lock_dir == bridge_home / "locks"
    assert (path, False, SID) in inspected
    assert (bridge_config, False, SID) in inspected
    assert (bridge_home / "locks", True, SID) in inspected
    assert all(item[0].name != "core-grants.json" for item in inspected)


def test_missing_core_private_grant_file_cannot_fallback_to_v1(tmp_path, monkeypatch):
    core, bridge, _document = _fixtures(tmp_path)
    home = tmp_path / "private-core"
    home.mkdir()
    monkeypatch.setattr(binding_module.storage, "home", lambda component, instance: home)

    def fail_missing(path, *, directory, sid):
        raise FileNotFoundError("missing private Core binding")

    monkeypatch.setattr(binding_module.storage, "check_private", fail_missing)
    with pytest.raises(CoreBridgeBindingError, match="invalid or not private"):
        load_native_core_bridge_binding(core, [])


def test_make_validator_pins_kernel_job_and_refuses_wrong_pid():
    calls = []

    def job_member(job_name, pid):
        calls.append((job_name, pid))
        return pid == 81

    check = make_bridge_peer_validator(
        bridge_id="bridge-shared", user_sid=SID, job_member=job_member
    )
    assert check(81)
    assert not check(82)
    assert not check(0)
    assert all(name == WindowsBridgeResources(SID, "bridge-shared").job_name for name, pid in calls)


@pytest.mark.parametrize("bridge_id", ["default", "../foreign", "WRONG", "", "a" * 33])
def test_validator_refuses_legacy_or_malformed_bridge_id(bridge_id):
    with pytest.raises((BridgePeerJobError, ValueError)):
        make_bridge_peer_validator(bridge_id=bridge_id, user_sid=SID)


def test_validator_requires_a_real_membership_verification_callback():
    with pytest.raises(BridgePeerJobError):
        make_bridge_peer_validator(bridge_id="bridge-shared", user_sid=SID, job_member=None)

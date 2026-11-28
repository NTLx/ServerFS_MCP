"""Pure nondefault Windows Bridge owner identity and resource contract tests.

No Windows APIs, Task Scheduler, private installed state or IPC are touched.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources
from serverfs_mcp.windows_v25.bridge_v2_owner import (
    BridgeV2OwnerError,
    bridge_workdir_claims_from_document,
    inspect_native_bridge_contract,
)
from serverfs_mcp.windows_v25.spec import Manifest
from serverfs_mcp.windows_v25.workdir_ownership import (
    WorkdirOwnershipError,
    assert_independent_bridge_roots,
)

SID = "S-1-5-21-111-222-333-1001"


def _fixture(tmp_path: Path, instance: str = "bridge-a"):
    owner_home = tmp_path / "v25" / "instances" / instance / "services" / "bridge"
    worker = (
        tmp_path
        / "v25"
        / "instances"
        / instance
        / "envs"
        / "agent-bridge"
        / "Scripts"
        / "python.exe"
    )
    resources = WindowsBridgeResources(SID, instance)
    manifest = Manifest.parse(
        {
            "schema": 2,
            "instance_id": instance,
            "component": "bridge",
            "sid": SID,
            "pythonw": str(
                tmp_path
                / "v25"
                / "instances"
                / instance
                / "envs"
                / "bridge"
                / "Scripts"
                / "pythonw.exe"
            ),
            "settings": {
                "config": str(owner_home / "bridge.json"),
                "bridge_python": str(worker),
            },
        }
    )
    config = {
        "bridge_id": instance,
        "core_grants_enabled": True,
        "allowed_peer_sid": SID,
        "socket_path": resources.pipe_name,
        "state_dir": str(owner_home / "state"),
        "lock_dir": str(owner_home / "locks"),
        "workdirs": [{"alias": "Repo"}],
    }
    grants = {
        "schema_version": 1,
        "bridge_id": instance,
        "grants": [
            {
                "core_id": "owner-health",
                "key_id": "key-a",
                "secret_hex": "cd" * 32,
                "methods": ["bridge.ping"],
                "workdirs": [{"alias": "Repo", "policy_sha256": "a" * 64}],
            }
        ],
    }
    return manifest, config, grants, owner_home, worker


def _plan(fixture):
    manifest, config, grants, home, worker = fixture
    return inspect_native_bridge_contract(
        manifest, config, grants, owner_home=home, worker_python=worker
    )


def test_distinct_bridges_get_independent_manifest_task_pipe_job_and_lease(tmp_path):
    first = _fixture(tmp_path, "bridge-a")
    second = _fixture(tmp_path, "bridge-b")
    a = _plan(first)
    b = _plan(second)
    assert a.bridge_id == "bridge-a"
    assert b.bridge_id == "bridge-b"
    assert first[0].task_name() != second[0].task_name()
    assert len({a.pipe_name, b.pipe_name}) == 2
    assert len({a.job_name, b.job_name}) == 2
    assert len({a.lease_artifact, b.lease_artifact}) == 2
    assert len({a.state_dir, b.state_dir}) == 2
    assert a.health_workdir == "Repo"
    assert a.health_secret == bytes.fromhex("cd" * 32)
    assert "cd" * 32 not in repr(a)
    assert Manifest.parse(first[0].to_dict()) == first[0]


@pytest.mark.parametrize(
    "change",
    [
        lambda cfg, g: cfg.update(bridge_id="other"),
        lambda cfg, g: cfg.update(core_grants_enabled=False),
        lambda cfg, g: cfg.update(allowed_peer_sid="S-1-5-21-1-2-3-4"),
        lambda cfg, g: cfg.update(socket_path=r"\\.\pipe\wrong-bridge"),
        lambda cfg, g: cfg.update(state_dir=r"C:\Other\bridge-state"),
        lambda cfg, g: cfg.update(lock_dir=r"C:\Other\bridge-locks"),
        lambda cfg, g: cfg.pop("state_dir"),
        lambda cfg, g: g.update(bridge_id="other"),
        lambda cfg, g: g.update(schema_version=True),
        lambda cfg, g: g.update(grants=[]),
        lambda cfg, g: g["grants"][0].update(methods=["task.submit"]),
        lambda cfg, g: g["grants"][0].update(secret_hex="weak"),
        lambda cfg, g: g["grants"][0].update(secret_hex="GG" * 32),
        lambda cfg, g: g["grants"][0].update(
            workdirs=[{"alias": "Other", "policy_sha256": "a" * 64}]
        ),
        lambda cfg, g: g["grants"][0]["workdirs"][0].update(policy_sha256="invalid"),
    ],
)
def test_unowned_or_ambiguous_health_configuration_fails_closed(tmp_path, change):
    fixture = _fixture(tmp_path)
    manifest, config, grants, home, worker = fixture
    config = copy.deepcopy(config)
    grants = copy.deepcopy(grants)
    change(config, grants)
    with pytest.raises(BridgeV2OwnerError):
        inspect_native_bridge_contract(
            manifest, config, grants, owner_home=home, worker_python=worker
        )


def test_nondefault_bridge_inputs_cannot_reuse_other_installs(tmp_path):
    a = _fixture(tmp_path, "bridge-a")
    b = _fixture(tmp_path, "bridge-b")
    with pytest.raises(BridgeV2OwnerError):
        inspect_native_bridge_contract(b[0], b[1], b[2], owner_home=a[3], worker_python=b[4])
    with pytest.raises(BridgeV2OwnerError):
        inspect_native_bridge_contract(a[0], a[1], a[2], owner_home=a[3], worker_python=b[4])


def test_two_v2_bridges_cannot_claim_overlapping_writable_workdirs():
    root = r"C:\\Shared\\Research"
    inner = r"C:\\Shared\\Research\\Nested"
    first = bridge_workdir_claims_from_document(
        "bridge-a",
        {"workdirs": [{"host_path": root, "read_only": False}]},
        canonicalize=lambda value: value,
    )
    second = bridge_workdir_claims_from_document(
        "bridge-b",
        {"workdirs": [{"host_path": inner, "read_only": True}]},
        canonicalize=lambda value: value,
    )
    with pytest.raises(WorkdirOwnershipError, match="overlap"):
        assert_independent_bridge_roots(second, first)
    read_only = bridge_workdir_claims_from_document(
        "bridge-c",
        {"workdirs": [{"host_path": root, "read_only": True}]},
        canonicalize=lambda value: value,
    )
    assert_independent_bridge_roots(second, read_only)


@pytest.mark.parametrize(
    "workdir",
    [
        {"host_path": "relative", "read_only": True},
        {"host_path": r"C:\\Shared\\..\\Other", "read_only": False},
        {"host_path": r"C:\\Shared", "read_only": "false"},
        {"host_path": r"C:\\Shared", "read_only": 0},
        {"host_path": None, "read_only": True},
    ],
)
def test_untrusted_bridge_claims_refuse_unsafe_roots_or_write_flags(workdir):
    with pytest.raises((BridgeV2OwnerError, WorkdirOwnershipError)):
        bridge_workdir_claims_from_document(
            "bridge-a", {"workdirs": [workdir]}, canonicalize=lambda value: value
        )


def test_untrusted_manifest_bridge_config_cannot_replace_default_owner(tmp_path):
    manifest, config, grants, home, worker = _fixture(tmp_path)
    altered = manifest.to_dict()
    altered["settings"]["bridge_python"] = str(tmp_path / "foreign" / "python.exe")
    with pytest.raises(BridgeV2OwnerError):
        inspect_native_bridge_contract(
            Manifest.parse(altered), config, grants, owner_home=home, worker_python=worker
        )

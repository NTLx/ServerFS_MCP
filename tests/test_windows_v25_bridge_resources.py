"""Windows v25 Bridge resource naming only, no Bridge/runtime launch."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from serverfs_mcp.instance_identity import InstanceIdentityError
from serverfs_mcp.native_endpoint import derive_pipe_name
from serverfs_mcp.windows_v25.bridge_resources import (
    BridgeIdentityError,
    WindowsBridgeResources,
)

SID = "S-1-5-21-111-222-333-1001"


def test_default_preserves_exact_v1_pipe_job_and_lease_identity() -> None:
    value = WindowsBridgeResources(SID, "default")
    legacy = derive_pipe_name(SID)
    digest = hashlib.sha256(legacy.encode("utf-8")).hexdigest()[:32]
    assert value.pipe_name == legacy
    assert value.job_name == "Global\\serverfs-agent-bridge-job-v1-" + digest
    assert value.lifecycle_artifact_name == ("serverfs-agent-lifecycle-" + digest + ".lifecycle")
    assert value.private_data_relative == Path("agent-bridge")
    assert value.lifecycle_root_relative == Path("agent-lifecycle")


def test_independent_bridges_have_disjoint_kernel_and_private_identities() -> None:
    values = [
        WindowsBridgeResources(SID, name)
        for name in (
            "default",
            "staging",
            "production",
        )
    ]
    for field in (
        "pipe_name",
        "job_name",
        "lifecycle_artifact_name",
        "private_data_relative",
        "lifecycle_root_relative",
    ):
        assert len({getattr(item, field) for item in values}) == 3
    assert values[1].pipe_name.startswith("\\\\.\\pipe\\serverfs-agent-bridge-v25-")
    assert values[1].job_name.startswith("Global\\serverfs-agent-bridge-job-v25-")
    assert values[1].private_data_relative == Path("v25/instances/staging/agent-bridge")
    assert values[1].lifecycle_root_relative == Path("v25/instances/staging/agent-lifecycle")


def test_same_bridge_id_but_other_user_sid_is_disjoint() -> None:
    other = "S-1-5-21-555-666-777-1001"
    a, b = WindowsBridgeResources(SID, "staging"), WindowsBridgeResources(other, "staging")
    assert a.pipe_name != b.pipe_name
    assert a.job_name != b.job_name
    assert a.lifecycle_artifact_name != b.lifecycle_artifact_name


def test_derivation_is_stable_across_objects() -> None:
    a = WindowsBridgeResources(SID, "staging")
    b = WindowsBridgeResources(SID, "staging")
    assert (a.pipe_name, a.job_name) == (b.pipe_name, b.job_name)


@pytest.mark.parametrize("sid", ["", "abc", "s-1-5-32", "S-/1", None, 3])
def test_invalid_sid_refused_before_any_host_action(sid: object) -> None:
    with pytest.raises(BridgeIdentityError):
        WindowsBridgeResources(sid, "staging")


@pytest.mark.parametrize("bridge_id", ["", "DEFAULT", "../escape", "with space", "a" * 33])
def test_invalid_bridge_id_refused_before_host_action(bridge_id: str) -> None:
    with pytest.raises(InstanceIdentityError):
        WindowsBridgeResources(SID, bridge_id)

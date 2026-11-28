"""Source-only Shared Bridge binding preflight; no service operations."""

from __future__ import annotations

import json
import shutil
import socket
import sys
import uuid
from pathlib import Path

import pytest

from serverfs_mcp.darwin_instance_shared_binding import (
    DarwinSharedBindingError,
    SharedBridgeBinding,
    SharedBridgeGrant,
    preflight_shared_core_binding,
)
from serverfs_mcp.instance_identity import InstanceIdentity

REVISION = "a" * 64
SECRET = b"s" * 32


@pytest.fixture
def fixture_binding(tmp_path: Path):
    if sys.platform != "darwin":
        pytest.skip("requires native Darwin /private/tmp and filesystem semantics")
    workdir = tmp_path / "workspace"
    workdir.mkdir(mode=0o700)
    config = tmp_path / "serverfs.toml"
    config.write_text(
        "[agent]\nenabled = true\n"
        "[agent.codex]\nenabled = true\n"
        "[[workdirs]]\nalias = 'workspace'\n"
        f"path = '{workdir}'\n"
        "read_only = false\n"
        "agent_mode = 'workspace-write'\n"
        "agent_runtimes = ['codex']\n"
    )
    config.chmod(0o600)
    bridge_config = tmp_path / "bridge.json"
    # Darwin's AF_UNIX sun_path is short; pytest's normal temporary path is
    # longer than the platform limit, so keep only the socket in /private/tmp.
    socket_dir = Path("/private/tmp") / f"sfb-{uuid.uuid4().hex[:10]}"
    socket_dir.mkdir(mode=0o700)
    socket_path = socket_dir / "b.sock"
    bridge_config.write_text(
        json.dumps(
            {
                "protocol_mode": "v2",
                "bridge_id": "shared-bridge",
                "socket_path": str(socket_path),
            }
        )
    )
    bridge_config.chmod(0o600)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    grant = SharedBridgeGrant(
        bridge_id="shared-bridge",
        core_id="named-core",
        key_id="core-key",
        secret=SECRET,
        workdir_alias="workspace",
        workdir_path=workdir,
        policy_revision=REVISION,
    )
    binding = SharedBridgeBinding(bridge_config, socket_path, grant, REVISION)
    yield workdir, config, bridge_config, socket_path, grant, binding
    listener.close()
    shutil.rmtree(socket_dir)


def test_accepts_isolated_synthetic_shared_binding(fixture_binding) -> None:
    _, config, _, socket_path, _, binding = fixture_binding
    identity = InstanceIdentity("named-core", "shared", "shared-bridge")
    assert preflight_shared_core_binding(identity, config, binding) == socket_path


@pytest.mark.parametrize(
    "identity",
    [
        InstanceIdentity("named-core", "disabled"),
        InstanceIdentity("default", "shared", "shared-bridge"),
        InstanceIdentity("named-core", "shared", "other-bridge"),
        InstanceIdentity("named-core", "owned", "named-core"),
    ],
)
def test_rejects_non_shared_or_wrong_bridge_identity(fixture_binding, identity) -> None:
    _, config, _, _, _, binding = fixture_binding
    with pytest.raises(DarwinSharedBindingError):
        preflight_shared_core_binding(identity, config, binding)


def test_rejects_foreign_workdir(fixture_binding, tmp_path: Path) -> None:
    _, config, _, _, grant, binding = fixture_binding
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    bad = SharedBridgeBinding(
        binding.bridge_config_path,
        binding.socket_path,
        SharedBridgeGrant(
            grant.bridge_id,
            grant.core_id,
            grant.key_id,
            grant.secret,
            grant.workdir_alias,
            foreign,
            grant.policy_revision,
        ),
        binding.core_policy_revision,
    )
    with pytest.raises(DarwinSharedBindingError, match="workdir identity"):
        preflight_shared_core_binding(
            InstanceIdentity("named-core", "shared", "shared-bridge"), config, bad
        )


def test_rejects_stale_digest_and_unknown_credential(fixture_binding) -> None:
    _, config, _, _, grant, binding = fixture_binding
    stale = SharedBridgeBinding(
        binding.bridge_config_path,
        binding.socket_path,
        grant,
        "b" * 64,
    )
    identity = InstanceIdentity("named-core", "shared", "shared-bridge")
    with pytest.raises(DarwinSharedBindingError, match="revision is stale"):
        preflight_shared_core_binding(identity, config, stale)
    unknown = SharedBridgeGrant(
        grant.bridge_id,
        "other-core",
        grant.key_id,
        grant.secret,
        grant.workdir_alias,
        grant.workdir_path,
        grant.policy_revision,
    )
    bad = SharedBridgeBinding(binding.bridge_config_path, binding.socket_path, unknown, REVISION)
    with pytest.raises(DarwinSharedBindingError, match="grant identity"):
        preflight_shared_core_binding(identity, config, bad)


@pytest.mark.parametrize("kind", ["symlink", "permissions"])
def test_rejects_unsafe_socket(fixture_binding, tmp_path: Path, kind: str) -> None:
    _, config, bridge_config, socket_path, _, binding = fixture_binding
    if kind == "symlink":
        socket_path.unlink()
        socket_path.symlink_to(tmp_path / "missing.sock")
    else:
        socket_path.chmod(0o666)
    with pytest.raises(DarwinSharedBindingError):
        preflight_shared_core_binding(
            InstanceIdentity("named-core", "shared", "shared-bridge"), config, binding
        )
    assert bridge_config.exists()


def test_secret_is_not_in_repr_or_errors(fixture_binding) -> None:
    _, config, bridge_config, socket_path, grant, _ = fixture_binding
    assert SECRET.decode() not in repr(grant)
    bridge_config.write_text("not json")
    with pytest.raises(DarwinSharedBindingError) as error:
        preflight_shared_core_binding(
            InstanceIdentity("named-core", "shared", "shared-bridge"),
            config,
            SharedBridgeBinding(bridge_config, socket_path, grant, REVISION),
        )
    assert SECRET.decode() not in str(error.value)


def test_owned_binding_implementation_remains_separate() -> None:
    from serverfs_mcp import darwin_instance_owned_binding

    assert darwin_instance_owned_binding.resolve_owned_core_binding.__name__ == (
        "resolve_owned_core_binding"
    )

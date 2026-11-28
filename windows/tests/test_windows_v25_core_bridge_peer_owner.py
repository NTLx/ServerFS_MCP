"""Nonsecret Bridge peer owner verification for independent Windows Core v2.

Core may inspect the protected Bridge policy, but it must never import or
open the Bridge owner's private HMAC grant material.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources
from serverfs_mcp.windows_v25.core_bridge_binding import (
    CoreBridgeBindingError,
    verify_core_peer_bridge_owner,
)
from serverfs_mcp.windows_v25.spec import Manifest

_SID = "S-1-5-21-111-222-333-1001"


def _peer(tmp_path: Path):
    bridge_id = "bridge-shared"
    home = tmp_path / "services" / "bridge"
    resources = WindowsBridgeResources(_SID, bridge_id)
    manifest = Manifest.parse(
        {
            "schema": 2,
            "instance_id": bridge_id,
            "component": "bridge",
            "sid": _SID,
            "pythonw": str(tmp_path / "envs" / "bridge" / "Scripts" / "pythonw.exe"),
            "settings": {
                "config": str(home / "bridge.json"),
                "bridge_python": str(tmp_path / "envs" / "agent-bridge" / "Scripts" / "python.exe"),
            },
        }
    )
    config = {
        "bridge_id": bridge_id,
        "core_grants_enabled": True,
        "socket_path": resources.pipe_name,
        "state_dir": str(home / "state"),
        "lock_dir": str(home / "locks"),
        "allowed_peer_sid": _SID,
        "workdirs": [],
    }
    return manifest, home, config


def test_verified_peer_identity_requires_only_nonsecret_config(tmp_path: Path) -> None:
    manifest, home, config = _peer(tmp_path)
    assert verify_core_peer_bridge_owner(manifest, config, owner_home=home) is None
    assert "secret_hex" not in repr(config)
    assert "core-grants.json" not in repr(config)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(bridge_id="different"),
        lambda d: d.update(core_grants_enabled=False),
        lambda d: d.pop("core_grants_enabled"),
        lambda d: d.update(socket_path=r"\\.\pipe\serverfs-agent-bridge-v1"),
        lambda d: d.update(state_dir=r"C:\Users\other\grants"),
        lambda d: d.update(lock_dir=r"C:\Users\other\locks"),
        lambda d: d.update(allowed_peer_sid="S-1-5-21-1-2-3-999"),
        lambda d: d.pop("socket_path"),
        lambda d: d.update(state_dir=12),
    ],
)
def test_cross_owner_endpoints_identity_and_state_are_denied(tmp_path: Path, mutate) -> None:
    manifest, home, config = _peer(tmp_path)
    altered = copy.deepcopy(config)
    mutate(altered)
    with pytest.raises(CoreBridgeBindingError, match="invalid or not private"):
        verify_core_peer_bridge_owner(manifest, altered, owner_home=home)


def test_default_bridge_or_wrong_component_is_not_a_v2_peer(tmp_path: Path) -> None:
    manifest, home, config = _peer(tmp_path)
    invalid = manifest.to_dict()
    invalid["component"] = "core"
    invalid["settings"] = {
        "config": str(home / "core.toml"),
        "auth_file": str(home / "http.env"),
        "hostname": "core.example.test",
        "port": 18871,
    }
    with pytest.raises(CoreBridgeBindingError):
        verify_core_peer_bridge_owner(Manifest.parse(invalid), config, owner_home=home)

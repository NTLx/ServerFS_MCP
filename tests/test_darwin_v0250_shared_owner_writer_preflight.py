"""Shared Core owner writer claims are enforced in owned-Bridge preflight.

A Shared Core owns no Bridge job, but its independently provisioned owner
still holds writer leases. These must be checked before adding an owned Bridge.
No live Bridge, socket or ServiceManagement process is necessary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_mcp.darwin_instance_bridge_identity import (
    DarwinBridgeIdentityError,
    preflight_named_owned_bridge,
)
from test_darwin_v0250_bridge_identity import (
    _candidate_owned_toml,
    _owned,
    _private_bridge_policy,
)
from test_darwin_v0250_shared_port_inventory import _base, _shared


@pytest.fixture
def owner(tmp_path: Path) -> dict:
    own_root = tmp_path / "owned-workspace"
    default_root = tmp_path / "default-workspace"
    shared_root = tmp_path / "shared-workspace"
    for root in (own_root, default_root, shared_root):
        root.mkdir()
    base = _base(tmp_path)
    socket = tmp_path / "private-owner.sock"
    owner_config = tmp_path / "private-owner.json"
    owner_config.write_text(
        json.dumps(
            {
                "protocol_mode": "v2",
                "bridge_id": "test-owner",
                "socket_path": str(socket),
                "workdirs": [
                    {"alias": "Workspace", "host_path": str(shared_root), "read_only": False}
                ],
            }
        ),
        encoding="utf-8",
    )
    owner_config.chmod(0o600)
    default = _private_bridge_policy(tmp_path / "default-bridge.json", default_root)
    candidate = _candidate_owned_toml(tmp_path, own_root)
    return {
        "home": tmp_path,
        "base": base,
        "candidate": candidate,
        "default": default,
        "owner_config": owner_config,
        "socket": socket,
        "shared_root": shared_root,
        "owned_root": own_root,
    }


def _bind(c: dict, name: str) -> Path:
    _shared(c["base"], name, 18111 if name == "core-a" else 18112)
    binding = c["base"] / "instances" / name / "service-manager" / "shared-bridge-binding.json"
    binding.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bridge_id": "test-owner",
                "core_id": name,
                "key_id": f"key-{name}",
                "secret_hex": "ab" * 32,
                "workdir_alias": "Workspace",
                "workdir_path": str(c["shared_root"]),
                "policy_revision": "e" * 64,
                "bridge_config_path": str(c["owner_config"]),
                "socket_path": str(c["socket"]),
            }
        ),
        encoding="utf-8",
    )
    binding.chmod(0o600)
    return binding


def _check(c: dict):
    return preflight_named_owned_bridge(
        _owned("new"),
        c["candidate"],
        home=c["home"],
        default_bridge_config=c["default"],
        runtime_root=Path("/private/tmp"),
    )


def test_owned_bridge_checks_single_shared_owner_writer_once_for_multiple_cores(owner) -> None:
    c = owner
    _bind(c, "core-a")
    _bind(c, "core-b")
    state = _check(c)
    assert state["result"] == "PASS_SOURCE_PREFLIGHT_ONLY"
    # Default Bridge plus ONE Shared owner, not two leases for the same owner.
    assert state["peer_bridge_policies_checked"] == 2


def test_owned_bridge_rejects_shared_owner_writable_overlap(owner) -> None:
    c = owner
    _bind(c, "core-a")
    c["candidate"] = _candidate_owned_toml(c["home"], c["shared_root"])
    with pytest.raises(DarwinBridgeIdentityError, match="overlapping writable"):
        _check(c)


@pytest.mark.parametrize(
    "tamper",
    ["wrong-owner", "wrong-socket", "public-binding", "missing-owner-config"],
)
def test_owned_bridge_fails_closed_on_unverifiable_shared_owner(owner, tamper: str) -> None:
    c = owner
    binding = _bind(c, "core-a")
    if tamper == "wrong-owner":
        doc = json.loads(c["owner_config"].read_text())
        doc["bridge_id"] = "different-owner"
        c["owner_config"].write_text(json.dumps(doc))
    elif tamper == "wrong-socket":
        doc = json.loads(c["owner_config"].read_text())
        doc["socket_path"] = "/private/tmp/unrelated.sock"
        c["owner_config"].write_text(json.dumps(doc))
    elif tamper == "public-binding":
        binding.chmod(0o644)
    else:
        c["owner_config"].unlink()
    with pytest.raises(DarwinBridgeIdentityError, match="shared owner Bridge policy"):
        _check(c)


def test_owner_bridge_policy_not_exposed_in_error(owner) -> None:
    c = owner
    _bind(c, "core-a")
    c["owner_config"].write_text('{"secret":"SHARED_OWNER_SECRET"}')
    with pytest.raises(DarwinBridgeIdentityError) as caught:
        _check(c)
    assert "SHARED_OWNER_SECRET" not in str(caught.value)

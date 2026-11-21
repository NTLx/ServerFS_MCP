"""I3 minimal read-only shared Bridge configuration, verified across both packages.

This is a deterministic source/configuration contract, not a staged Windows
service. Neither Core can submit Agent tasks or write files through Bridge.
The single shared review-only policy permits signed v2 health/runtimes
queries while preserving the still-active legacy Bridge write owner.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.native_config import load_native_config
from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources
from serverfs_mcp.windows_v25.core_bridge_binding import (
    parse_core_bridge_binding,
    verify_core_bridge_workdirs,
    verify_core_peer_bridge_owner,
)
from serverfs_mcp.windows_v25.shared_core_grants import issue_shared_core_capabilities
from serverfs_mcp.windows_v25.spec import Manifest

SID = "S-1-5-21-111-222-333-1001"
BRIDGE = "i3-shared"
CORES = ("i3-core-a", "i3-core-b")


def _manifest(tmp_path: Path, *, component: str, instance: str):
    folder = tmp_path / instance
    settings = (
        {
            "config": str(folder / "bridge.json"),
            "bridge_python": str(folder / "envs" / "worker" / "Scripts" / "python.exe"),
        }
        if component == "bridge"
        else {
            "config": str(folder / "serverfs.toml"),
            "auth_file": str(folder / "http.env"),
            "port": 18869 if instance == CORES[0] else 18870,
            "hostname": f"{instance}.local",
        }
    )
    return Manifest.parse(
        {
            "schema": 2,
            "instance_id": instance,
            "component": component,
            "sid": SID,
            "pythonw": str(folder / "envs" / "owner" / "Scripts" / "pythonw.exe"),
            "settings": settings,
        }
    )


def _config(tmp_path: Path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    peer = tmp_path / BRIDGE
    peer.mkdir()
    (peer / "state").mkdir()
    (peer / "locks").mkdir()
    resources = WindowsBridgeResources(SID, BRIDGE)
    document = {
        "bridge_id": BRIDGE,
        "core_grants_enabled": True,
        "socket_path": resources.pipe_name,
        "state_dir": str(peer / "state"),
        "lock_dir": str(peer / "locks"),
        "lease_key": "alias",
        "allowed_peer_sid": SID,
        "workdirs": [
            {
                "alias": "Fixture",
                "host_path": str(fixture),
                "read_only": True,
                "agent_mode": "review",
                "agent_runtimes": [],
                "repo_ops_enabled": False,
            }
        ],
    }
    (peer / "bridge.json").write_text(json.dumps(document), encoding="utf-8")
    return document, peer, fixture


@pytest.mark.skipif(
    sys.platform != "win32", reason="requires Windows-only Bridge grant configuration"
)
def test_two_cores_share_one_bridge_without_enabling_a_runtime_or_write(
    tmp_path: Path, monkeypatch
) -> None:
    # Bridge is a distinct source distribution; the repo .venv installs only Core.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "agent_bridge" / "src"))
    from serverfs_agent_bridge.config import BridgeConfig
    from serverfs_agent_bridge.core_grants import core_workdir_policy_digest

    document, peer, fixture = _config(tmp_path)
    config = BridgeConfig.load(peer / "bridge.json")
    policy = config.policies.get("Fixture")
    assert policy is not None
    assert policy.mode.value == "review"
    assert policy.read_only is True
    assert policy.runtimes == frozenset()
    assert config.core_grants_enabled is True
    assert config.enable_fake_runtime is False
    assert config.codex.enabled is False
    assert config.claude.enabled is False
    bridge_manifest = _manifest(tmp_path, component="bridge", instance=BRIDGE)
    verify_core_peer_bridge_owner(bridge_manifest, document, owner_home=peer)

    revisions = {"Fixture": core_workdir_policy_digest(policy)}
    issued = issue_shared_core_capabilities(
        bridge_id=BRIDGE,
        core_ids=CORES,
        workdir_policy_revisions=revisions,
        methods=("bridge.ping", "runtime.list"),
    )
    assert len(issued) == 2
    assert issued[0].secret != issued[1].secret
    assert issued[0].key_id != issued[1].key_id

    for core_id, capability in zip(CORES, issued, strict=True):
        core_dir = tmp_path / core_id
        core_dir.mkdir()
        toml = (
            '[server]\nlog_level = "INFO"\n\n'
            "[agent]\nenabled = true\n\n"
            '[[workdirs]]\nalias = "Fixture"\n'
            f"path = {json.dumps(str(fixture))}\n"
            'read_only = true\nagent_mode = "review"\n'
            "agent_runtimes = []\nrepo_ops_enabled = false\n"
        )
        native = core_dir / "serverfs.toml"
        native.write_text(toml, encoding="utf-8")
        workdirs, settings = load_native_config(native)
        assert settings.bridge_required is True
        assert settings.repo_ops_enabled is False
        assert len(workdirs) == 1
        verify_core_bridge_workdirs(
            workdirs, document, canonicalize=lambda raw: Path(raw).resolve(strict=True)
        )
        binding = parse_core_bridge_binding(
            _manifest(tmp_path, component="core", instance=core_id),
            capability.core_binding(),
            local_aliases=frozenset({"Fixture"}),
            verified_bridge=bridge_manifest,
            bridge_lock_dir=peer / "locks",
        )
        assert binding.bridge_mode == "shared"
        assert binding.bridge_id == BRIDGE
        assert binding.policy_revisions == (("Fixture", revisions["Fixture"]),)
        settings, client = binding.build_wiring(peer_validator=lambda *_args: True)
        assert settings.agent_bridge_enabled is True
        assert client is not None


@pytest.mark.parametrize("change", [{"read_only": False}, {"agent_mode": "disabled"}])
def test_insecure_scope_changes_are_denied_before_shared_binding(tmp_path: Path, change) -> None:
    from serverfs_mcp.windows_v25.core_bridge_binding import CoreBridgeBindingError

    document, _, fixture = _config(tmp_path)
    document["workdirs"][0].update(change)
    local = [
        SimpleNamespace(
            alias="Fixture",
            root=fixture,
            read_only=True,
            agent_mode="review",
            agent_runtimes=frozenset(),
            policy=SimpleNamespace(repo_ops_enabled=False),
        )
    ]
    with pytest.raises(CoreBridgeBindingError):
        verify_core_bridge_workdirs(
            local, document, canonicalize=lambda raw: Path(raw).resolve(strict=True)
        )

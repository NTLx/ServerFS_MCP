"""macOS v0.25 Core -> separate Bridge config renderer staging contract."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from serverfs_mcp import darwin_instance_cli, darwin_instance_owned_staging
from serverfs_mcp.darwin_instance_owned_staging import (
    DarwinOwnedPolicyError,
    check_owned_bridge_policy,
    stage_owned_bridge_policy,
)
from serverfs_mcp.instance_identity import InstanceIdentity

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
BRIDGE_PYTHON = Path(__file__).resolve().parents[1] / "agent_bridge/.venv/bin/python"


def _candidate(tmp_path: Path, identifier: str = "candidate"):
    user_home = tmp_path / "user"
    root = (
        user_home
        / "Library/Application Support/ServerFS/instances"
        / identifier
        / "service-manager"
    )
    (root / "data").mkdir(parents=True, mode=0o700)
    for parent in (root.parent.parent, root.parent, root, root / "data"):
        parent.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    operator = tmp_path / "operator.toml"
    operator.write_text(
        "[agent]\nenabled = true\n"
        "[agent.codex]\nenabled = true\n"
        '[[workdirs]]\nalias = "workspace"\n'
        f'path = "{workspace}"\n'
        "read_only = false\n"
        'agent_mode = "workspace-write"\n'
        'agent_runtimes = ["codex"]\n'
    )
    default_root = tmp_path / "default-workspace"
    default_root.mkdir()
    default = tmp_path / "default-bridge.json"
    default.write_text(
        json.dumps(
            {
                "workdirs": [
                    {
                        "alias": "production",
                        "host_path": str(default_root),
                        "read_only": False,
                    }
                ]
            }
        )
    )
    default.chmod(0o600)
    return (
        InstanceIdentity(instance_id=identifier, bridge_mode="owned", bridge_id=identifier),
        operator,
        default,
        user_home,
        root,
        workspace,
    )


def test_named_owned_stage_uses_real_bridge_package_and_validates_policy(
    tmp_path: Path,
) -> None:
    if not BRIDGE_PYTHON.exists():
        pytest.skip("independent Bridge Python environment not installed")
    identity, config, default, home, root, workdir = _candidate(tmp_path)
    result = stage_owned_bridge_policy(
        identity,
        config,
        BRIDGE_PYTHON,
        home=home,
        default_bridge_config=default,
    )
    assert result == {
        "instance_id": "candidate",
        "state": "staged-not-registered",
        "bridge_mode": "owned",
        "workdirs": 1,
        "private_config": "verified",
    }
    policy_path = root / "data/agent-bridge/bridge.json"
    assert stat.S_IMODE(policy_path.stat().st_mode) == 0o600
    doc = json.loads(policy_path.read_text())
    assert doc["workdirs"][0]["alias"] == "workspace"
    assert Path(doc["workdirs"][0]["host_path"]) == workdir
    assert doc["allowed_peer_uid"] == os.getuid()
    assert "allowed_peer_gid" not in doc
    assert doc["lease_key"] == "alias"
    assert doc["codex"]["enabled"] is True
    assert doc["enable_fake_runtime"] is False
    assert "jev" not in doc and "proxy" not in doc
    assert not (root / "services.json").exists()
    before = policy_path.read_bytes()
    assert check_owned_bridge_policy(
        identity,
        config,
        BRIDGE_PYTHON,
        home=home,
        default_bridge_config=default,
    ) == {
        "instance_id": "candidate",
        "bridge_mode": "owned",
        "policy": "synced",
        "workdirs": 1,
        "result": "PASS_READ_ONLY",
    }
    assert policy_path.read_bytes() == before


def test_named_owned_check_refuses_toml_drift_and_preserves_config(
    tmp_path: Path,
) -> None:
    if not BRIDGE_PYTHON.exists():
        pytest.skip("independent Bridge Python environment not installed")
    identity, config, default, home, root, _ = _candidate(tmp_path)
    stage_owned_bridge_policy(
        identity,
        config,
        BRIDGE_PYTHON,
        home=home,
        default_bridge_config=default,
    )
    private = root / "data/agent-bridge/bridge.json"
    before = private.read_bytes()
    original_toml = config.read_text()
    changed_toml = original_toml.replace(
        "enabled = true", "enabled = true" + chr(10) + "max_active_tasks = 2", 1
    )
    assert changed_toml != original_toml
    config.write_text(changed_toml)
    with pytest.raises(DarwinOwnedPolicyError, match="stale"):
        check_owned_bridge_policy(
            identity,
            config,
            BRIDGE_PYTHON,
            home=home,
            default_bridge_config=default,
        )
    assert private.read_bytes() == before


def test_named_owned_stage_denies_existing_instance_manifest(
    tmp_path: Path,
) -> None:
    identity, config, default, home, root, _ = _candidate(tmp_path)
    (root / "services.json").write_text('{"bridge_mode":"disabled"}')
    with pytest.raises(DarwinOwnedPolicyError, match="overwrite"):
        stage_owned_bridge_policy(
            identity,
            config,
            BRIDGE_PYTHON,
            home=home,
            default_bridge_config=default,
        )
    assert not (root / "data/agent-bridge").exists()


def test_named_owned_stage_denies_missing_bridge_python(
    tmp_path: Path,
) -> None:
    identity, config, default, home, root, _ = _candidate(tmp_path)
    with pytest.raises(DarwinOwnedPolicyError, match="interpreter"):
        stage_owned_bridge_policy(
            identity,
            config,
            tmp_path / "not-installed",
            home=home,
            default_bridge_config=default,
        )
    assert not (root / "data/agent-bridge").exists()


def test_named_owned_policy_check_cli_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    observed = []

    def check(identity, config, python):
        observed.append((identity.bridge_mode, identity.bridge_id, config, python))
        return {
            "instance_id": identity.instance_id,
            "policy": "synced",
            "result": "PASS_READ_ONLY",
        }

    monkeypatch.setattr(darwin_instance_owned_staging, "check_owned_bridge_policy", check)
    assert (
        darwin_instance_cli.main(
            [
                "--instance",
                "candidate",
                "owned-policy-check",
                "--config",
                str(tmp_path / "owner.toml"),
                "--bridge-python",
                str(BRIDGE_PYTHON),
            ]
        )
        == 0
    )
    assert observed == [("owned", "candidate", tmp_path / "owner.toml", BRIDGE_PYTHON)]
    assert json.loads(capsys.readouterr().out)["result"] == "PASS_READ_ONLY"


def test_named_owned_stage_denies_nonowned_identity(tmp_path: Path) -> None:
    _, config, default, home, root, _ = _candidate(tmp_path)
    with pytest.raises(DarwinOwnedPolicyError, match="independently owned"):
        stage_owned_bridge_policy(
            InstanceIdentity(instance_id="candidate"),
            config,
            BRIDGE_PYTHON,
            home=home,
            default_bridge_config=default,
        )
    assert not (root / "data/agent-bridge").exists()

"""Protected v2 Core grants: private-only loading, policy pinning and failure modes.

A temporary TaskStore creates the same protected state home used by Bridge.
No real Bridge IPC listener, Windows Task Scheduler, provider or network is used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
from serverfs_agent_bridge.core_grants_private import load_private_core_grants
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.store import TaskStore
from test_core_grants import _request


def _fixture(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    policy = WorkdirAgentPolicy(slot=None, alias="Repo", host_path=root, mode=AgentMode.DISABLED)
    policies = PolicyRegistry([policy])
    store = TaskStore(tmp_path / "private-state")
    data = {
        "schema_version": 1,
        "bridge_id": "bridge-v2",
        "grants": [
            {
                "core_id": "core-a",
                "key_id": "key-a",
                "secret_hex": "ab" * 32,
                "methods": ["repo.inspect", "task.submit", "task.get"],
                "workdirs": [
                    {
                        "alias": "Repo",
                        "policy_sha256": core_workdir_policy_digest(policy),
                    }
                ],
            }
        ],
    }
    return store, policies, data


def _write(store: TaskStore, data) -> Path:
    path = store.state_dir / "core-grants.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return path


def _load(store, policies):
    return load_private_core_grants(store=store, bridge_id="bridge-v2", policies=policies)


def test_grants_are_verified_and_bound_to_durable_replay(tmp_path: Path) -> None:
    store, policies, data = _fixture(tmp_path)
    _write(store, data)
    verifier = _load(store, policies)
    assert verifier.durable_replay_protection
    assert len(verifier._grants) == 1
    grant = next(iter(verifier._grants.values()))
    request = _request(
        grant,
        bridge_id="bridge-v2",
        workdir="Repo",
        policy=data["grants"][0]["workdirs"][0]["policy_sha256"],
        nonce="e" * 32,
    )
    assert verifier.authorize(request, now=1_800_000_000).core_id == "core-a"
    # Reloaded verifier must still reject a replay of the same signed request.
    with pytest.raises(Exception) as blocked:
        _load(TaskStore(store.state_dir), policies).authorize(request, now=1_800_000_000)
    assert getattr(blocked.value, "code", None) == "CORE_NOT_AUTHORIZED"


def test_missing_file_must_fail_closed_without_creation(tmp_path: Path) -> None:
    store, policies, _data = _fixture(tmp_path)
    path = store.state_dir / "core-grants.json"
    assert not path.exists()
    with pytest.raises(ValueError, match="invalid or not private"):
        _load(store, policies)
    assert not path.exists()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda d: d.update(bridge_id="other-bridge"),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(extra_field=1),
        lambda d: d.update(grants="not-an-array"),
        lambda d: d["grants"].append(dict(d["grants"][0])),
        lambda d: d["grants"][0].update(core_id="Invalid_Core"),
        lambda d: d["grants"][0].update(key_id="../escape"),
        lambda d: d["grants"][0].update(secret_hex="short"),
        lambda d: d["grants"][0].update(secret_hex="GG" * 32),
        lambda d: d["grants"][0].update(methods=["task.get", "task.get"]),
        lambda d: d["grants"][0].update(methods=["unknown"]),
        lambda d: d["grants"][0].update(workdirs=[{"alias": "Other", "policy_sha256": "b" * 64}]),
        lambda d: d["grants"][0]["workdirs"][0].update(policy_sha256="b" * 64),
        lambda d: d["grants"][0].update(host_path="/unexpected"),
    ],
)
def test_invalid_private_grants_fail_without_secret_leak(tmp_path: Path, mutator) -> None:
    store, policies, data = _fixture(tmp_path)
    mutator(data)
    _write(store, data)
    with pytest.raises(ValueError) as caught:
        _load(store, policies)
    assert "ab" * 32 not in str(caught.value)
    assert str(caught.value) == "Bridge Core grants file is invalid or not private"


def test_duplicate_json_keys_rejected_not_last_one_wins(tmp_path: Path) -> None:
    store, policies, _ = _fixture(tmp_path)
    path = store.state_dir / "core-grants.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="invalid or not private"):
        _load(store, policies)


def test_oversized_grants_file_rejected(tmp_path: Path) -> None:
    store, policies, _data = _fixture(tmp_path)
    path = store.state_dir / "core-grants.json"
    path.write_text("x" * (128 * 1024 + 1), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="invalid or not private"):
        _load(store, policies)


def test_grants_file_symlink_is_rejected_even_if_target_is_private(tmp_path: Path) -> None:
    store, policies, data = _fixture(tmp_path)
    target = tmp_path / "outside-private.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    target.chmod(0o600)
    path = store.state_dir / "core-grants.json"
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("current account cannot create file symlinks")
    with pytest.raises(ValueError, match="invalid or not private"):
        _load(store, policies)


def test_grant_is_invalid_if_bridge_workdir_policy_changed(tmp_path: Path) -> None:
    store, policies, data = _fixture(tmp_path)
    _write(store, data)
    assert _load(store, policies).durable_replay_protection
    old = policies.get("Repo")
    replaced = WorkdirAgentPolicy(
        slot=None,
        alias="Repo",
        host_path=old.host_path,
        mode=AgentMode.DISABLED,
        read_only=False,
    )
    with pytest.raises(ValueError, match="invalid or not private"):
        _load(store, PolicyRegistry([replaced]))


def test_protected_grant_file_does_not_accept_posix_world_readable(
    tmp_path: Path,
) -> None:
    import os

    if os.name == "nt":
        pytest.skip("Windows ACL, rather than POSIX mode, determines private files")
    store, policies, data = _fixture(tmp_path)
    path = _write(store, data)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="invalid or not private"):
        _load(store, policies)

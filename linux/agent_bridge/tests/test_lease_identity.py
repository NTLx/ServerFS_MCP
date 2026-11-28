"""The lease-identity contract on the Bridge side (v0.11 §5.3, §5.5).

The vector table is duplicated verbatim from ``tests/test_lease_identity.py`` on the ServerFS side:
the two packages must not import each other (§23), so cross-boundary agreement is pinned by shared
data rather than by a shared function.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.lease_identity import (
    ARTIFACT_HASH_LENGTH,
    LOCK_SUFFIX,
    MAX_WORKDIR_SLOTS,
    LeaseIdentityError,
    alias_lease_id,
    guard_artifact_name,
    is_guard_artifact_name,
    lock_artifact_name,
    slot_lease_id,
    slot_of,
    validate_lease_id,
)
from serverfs_agent_bridge.models import AgentMode, TaskRecord
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy

VECTORS = [
    ("slot:01", "01"),
    ("slot:09", "09"),
    ("slot:16", "16"),
    ("alias:repo", "255b6be1a22d261f7c81e80e37b195e16c9212de"),
    ("alias:Foo", "8665896a714f759ccc3ca905910d92cd3607e079"),
    ("alias:foo", "62cc1bea70d19330680ae63a48d871f25900549f"),
    ("alias:con", "da499902b64252157afa2e0c9b43afb5d7710cfa"),
    ("alias:a b", "308002eb6850cf6965196dd5ec61b943d771964a"),
    ("alias:..", "5ef1397ecf813e259b51567c3c15754f2cc9bf0c"),
    ("alias:" + "x" * 200, "5c89426d94acbb35e1524ad2c87e8784ccec008d"),
    ("alias:中文", "96870bc5ff80257d6c9e6ac325b80dfb6f45395d"),
]


@pytest.mark.parametrize(("lease_id", "stem"), VECTORS)
def test_artifact_names_match_the_serverfs_side(lease_id: str, stem: str) -> None:
    assert lock_artifact_name(lease_id) == f"{stem}.lock"
    assert guard_artifact_name(lease_id) == stem


@pytest.mark.parametrize("alias", ["con", "nul", "a b", "x" * 63, "中文"])
def test_alias_derived_names_are_safe_and_bounded(alias: str) -> None:
    name = lock_artifact_name(alias_lease_id(alias))
    assert len(name) == ARTIFACT_HASH_LENGTH + len(LOCK_SUFFIX)
    assert alias not in name


@pytest.mark.parametrize(("lease_id", "stem"), VECTORS)
def test_guard_artifact_names_are_recognised(lease_id: str, stem: str) -> None:
    assert is_guard_artifact_name(stem) is True
    assert is_guard_artifact_name(f"{stem}.lock") is False
    assert is_guard_artifact_name(f".{stem}.abc123.tmp") is False


@pytest.mark.parametrize(
    "junk", ["", "00", "17", "1", "abc", "0" * 39, "0" * 41, "g" * 40, "x" * 41]
)
def test_non_guard_names_are_not_recovery_state(junk: str) -> None:
    assert is_guard_artifact_name(junk) is False


@pytest.mark.parametrize(
    "rejected",
    [
        "",
        "repo",
        "slot:00",
        "slot:17",
        "slot:1",
        "other:repo",
        "alias:",
        "alias:bad/name",
        "alias:bad\\name",
        "alias:control\x01char",
        "alias:" + "x" * 300,
    ],
)
def test_malformed_lease_ids_fail_closed(rejected: str) -> None:
    with pytest.raises(LeaseIdentityError):
        validate_lease_id(rejected)
    with pytest.raises(LeaseIdentityError):
        lock_artifact_name(rejected)


def test_slot_of_separates_the_two_kinds() -> None:
    assert slot_of(slot_lease_id(3)) == 3
    assert slot_of(alias_lease_id("repo")) is None


def test_policy_derives_its_lease_id(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    legacy = WorkdirAgentPolicy(slot=3, alias="repo", host_path=root, mode=AgentMode.REVIEW)
    native = WorkdirAgentPolicy(slot=None, alias="Repo", host_path=root, mode=AgentMode.REVIEW)
    assert legacy.lease_id == "slot:03"
    assert native.lease_id == "alias:Repo"
    assert lock_artifact_name(native.lease_id) != lock_artifact_name(
        WorkdirAgentPolicy(slot=None, alias="repo", host_path=root, mode=AgentMode.REVIEW).lease_id
    )


def test_registry_rejects_two_workdirs_sharing_one_lease(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError, match="duplicate workdir lease"):
        PolicyRegistry(
            [
                WorkdirAgentPolicy(slot=2, alias="one", host_path=root, mode=AgentMode.REVIEW),
                WorkdirAgentPolicy(slot=2, alias="two", host_path=root, mode=AgentMode.REVIEW),
            ]
        )


def test_registry_lists_every_configured_lease(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    registry = PolicyRegistry(
        [
            WorkdirAgentPolicy(slot=2, alias="one", host_path=root, mode=AgentMode.REVIEW),
            WorkdirAgentPolicy(slot=None, alias="two", host_path=root, mode=AgentMode.REVIEW),
        ]
    )
    assert registry.lease_ids() == ("slot:02", "alias:two")


def test_task_record_recomputes_the_lease_of_a_legacy_row() -> None:
    record = _record(workdir_slot=5, workdir_alias="repo")
    assert record.lease_id == "slot:05"


def test_task_record_recomputes_the_lease_of_a_native_row() -> None:
    record = _record(workdir_slot=0, workdir_alias="Repo")
    assert record.lease_id == "alias:Repo"


def _record(**overrides: Any) -> TaskRecord:
    fields: dict[str, Any] = {
        "task_id": "agt_1",
        "runtime": "fake",
        "workdir_alias": "repo",
        "workdir_slot": 1,
        "relative_cwd": ".",
        "profile": "workspace-write",
        "requested_model": None,
        "status": "queued",
        "created_at": "2026-10-05T00:00:00Z",
        "started_at": None,
        "updated_at": "2026-10-05T00:00:00Z",
        "completed_at": None,
        "deadline_at": None,
        "continue_from_task_id": None,
        "correlation_id": None,
        "idempotency_key": None,
        "request_fingerprint": None,
        "native_session_id": None,
        "native_turn_id": None,
        "final_response": None,
        "result_storage": "inline",
        "result_size_bytes": None,
        "result_sha256": None,
        "manifest": None,
        "manifest_sha256": None,
        "error_code": None,
        "error_message": None,
        "pending_request_id": None,
    }
    fields.update(overrides)
    return TaskRecord(**fields)


def _write_config(tmp_path: Path, workdirs: list[dict[str, Any]], **top: Any) -> Path:
    path = tmp_path / "bridge.json"
    path.write_text(
        json.dumps({"workdirs": workdirs, "enable_fake_runtime": True, **top}), encoding="utf-8"
    )
    return path


def _workdir_entry(tmp_path: Path, alias: str, **extra: Any) -> dict[str, Any]:
    root = tmp_path / alias
    root.mkdir()
    entry: dict[str, Any] = {
        "alias": alias,
        "host_path": str(root),
        "read_only": False,
        "agent_mode": AgentMode.WORKSPACE_WRITE.value,
        "agent_runtimes": ["fake"],
    }
    entry.update(extra)
    return entry


def test_config_defaults_to_the_legacy_slot_layout(tmp_path: Path) -> None:
    config = BridgeConfig.load(_write_config(tmp_path, [_workdir_entry(tmp_path, "repo", slot=1)]))
    assert config.policies.lease_ids() == ("slot:01",)


def test_alias_lease_key_rejects_slots(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="workdir.slot is not used"):
        BridgeConfig.load(
            _write_config(
                tmp_path,
                [_workdir_entry(tmp_path, "repo", slot=1)],
                lease_key="alias",
            )
        )


def test_alias_lease_key_derives_workdir_slots_as_none(tmp_path: Path) -> None:
    # One host directory, two aliases that differ only by case: a case-folding filesystem makes
    # these the same object, which is exactly why the artifact name must not be the alias text.
    entry = _workdir_entry(tmp_path, "Repo")
    second = {**entry, "alias": "repo"}
    config = BridgeConfig.load(
        _write_config(tmp_path, [entry, second], lease_key="alias"),
    )
    assert config.policies.lease_ids() == ("alias:Repo", "alias:repo")
    names = {lock_artifact_name(lease) for lease in config.policies.lease_ids()}
    assert len(names) == 2


def test_slot_lease_key_still_requires_a_slot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="workdir.slot"):
        BridgeConfig.load(_write_config(tmp_path, [_workdir_entry(tmp_path, "repo")]))


def test_lease_key_is_a_closed_set(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lease_key"):
        BridgeConfig.load(
            _write_config(tmp_path, [_workdir_entry(tmp_path, "repo", slot=1)], lease_key="path")
        )


def test_slot_range_is_unchanged(tmp_path: Path) -> None:
    assert MAX_WORKDIR_SLOTS == 16
    with pytest.raises(ValueError, match="workdir.slot must be between 1 and 16"):
        BridgeConfig.load(_write_config(tmp_path, [_workdir_entry(tmp_path, "repo", slot=17)]))

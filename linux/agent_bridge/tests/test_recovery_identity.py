"""Recovery-guard identity rules (v0.11 §5.4, C0 completion contract).

Guards are the persistent half of the writer lease: they survive a crash precisely because they are
ordinary files on disk, so their identity rules are security-relevant rather than cosmetic. A guard
may not claim a workdir other than the artifact it occupies, an unreadable guard is a recovery
condition instead of something to skip, and a guard written by v0.10 — which named only a slot —
must stay readable and clearable after an upgrade.

Portable by design: the rules are the same on both backends, so the Linux gate runs them too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.lease_identity import (
    alias_lease_id,
    guard_artifact_name,
    slot_lease_id,
)
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.recovery import ActiveGuardManager

REPO = alias_lease_id("repo")
OTHER = alias_lease_id("other")


@pytest.fixture()
def guards(tmp_path: Path) -> ActiveGuardManager:
    manager = LeaseManager(tmp_path / "locks", lease_ids=[REPO])
    return ActiveGuardManager(manager.lock_dir)


def create(manager: ActiveGuardManager, lease_id: str, task_id: str) -> None:
    manager.create(
        lease_id=lease_id,
        task_id=task_id,
        runtime="fake",
        workdir_alias="repo",
        correlation_id=None,
    )


def write_raw(manager: ActiveGuardManager, name: str, payload: dict) -> Path:
    path = manager.guard_dir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_guard_records_both_the_lease_and_the_legacy_slot(tmp_path: Path) -> None:
    manager = LeaseManager(tmp_path / "locks", lease_ids=[slot_lease_id(7)])
    guards = ActiveGuardManager(manager.lock_dir)
    create(guards, slot_lease_id(7), "agt_slot")
    # v0.10 artifact names survive: same file, same directory layout.
    assert (guards.guard_dir / "07").is_file()
    guard = guards.read(slot_lease_id(7))
    assert guard is not None
    assert guard.lease_id == "slot:07"
    assert guard.payload["slot"] == 7
    assert guard.payload["lease_id"] == "slot:07"


def test_an_alias_guard_has_a_hashed_name_and_no_slot(tmp_path: Path) -> None:
    manager = LeaseManager(tmp_path / "locks", lease_ids=[REPO])
    guards = ActiveGuardManager(manager.lock_dir)
    create(guards, REPO, "agt_alias")
    path = guards.guard_dir / guard_artifact_name(REPO)
    assert path.is_file()
    assert path.name != "repo"
    assert len(path.name) == 40
    guard = guards.read(REPO)
    assert guard is not None
    assert guard.payload["slot"] is None


def test_a_legacy_slot_only_guard_is_readable_and_clearable(tmp_path: Path) -> None:
    """Upgrade path: a guard written before alias-keyed leases must not become unclearable."""
    manager = LeaseManager(tmp_path / "locks", lease_ids=[slot_lease_id(3)])
    guards = ActiveGuardManager(manager.lock_dir)
    write_raw(
        guards,
        "03",
        {
            "schema_version": 1,
            "slot": 3,
            "task_id": "agt_old",
            "runtime": "codex",
            "workdir_alias": "repo",
            "correlation_id": None,
            "created_at": "2026-10-04T00:00:00Z",
            "updated_at": "2026-10-04T00:00:00Z",
        },
    )
    guard = guards.read(slot_lease_id(3))
    assert guard is not None
    assert guard.lease_id == "slot:03"
    guards.remove(lease_id=slot_lease_id(3), task_id="agt_old")
    assert guards.read(slot_lease_id(3)) is None


def test_a_guard_cannot_claim_another_workdir(guards: ActiveGuardManager) -> None:
    """The artifact name is derived from the lease id, so the pair has to agree."""
    write_raw(
        guards,
        guard_artifact_name(REPO),
        {
            "schema_version": 1,
            "lease_id": OTHER,
            "slot": None,
            "task_id": "agt_mismatch",
            "runtime": "fake",
            "workdir_alias": "other",
        },
    )
    with pytest.raises(BridgeError) as exc:
        guards.read(REPO)
    assert exc.value.code == "WORKDIR_RECOVERY_REQUIRED"
    assert "invalid" in str(exc.value)


def test_a_slot_payload_that_disagrees_with_its_own_lease_is_invalid(guards) -> None:
    write_raw(
        guards,
        guard_artifact_name(REPO),
        {
            "schema_version": 1,
            "lease_id": REPO,
            "slot": 9,
            "task_id": "agt_contradiction",
            "runtime": "fake",
            "workdir_alias": "repo",
        },
    )
    with pytest.raises(BridgeError) as exc:
        guards.read(REPO)
    assert "invalid" in str(exc.value)


def test_the_scan_finds_guards_and_ignores_foreign_entries(guards: ActiveGuardManager) -> None:
    create(guards, REPO, "agt_scan")
    stray = write_raw(
        guards,
        guard_artifact_name(OTHER),
        {
            "schema_version": 1,
            "lease_id": OTHER,
            "slot": None,
            "task_id": "agt_other",
            "runtime": "fake",
            "workdir_alias": "other",
        },
    )
    (guards.guard_dir / ".unreadable.tmp").write_text("{}", encoding="utf-8")
    (guards.guard_dir / "notes.json").write_text("{}", encoding="utf-8")
    found = {guard.lease_id: guard.payload["task_id"] for guard in guards.list()}
    assert found == {REPO: "agt_scan", OTHER: "agt_other"}
    assert stray.name == guard_artifact_name(OTHER)


def test_an_unreadable_guard_is_a_recovery_condition_not_a_skip(guards: ActiveGuardManager) -> None:
    create(guards, REPO, "agt_live")
    (guards.guard_dir / guard_artifact_name(REPO)).write_text("{ not json", encoding="utf-8")
    with pytest.raises(BridgeError) as exc:
        guards.list()
    assert exc.value.code == "WORKDIR_RECOVERY_REQUIRED"


def test_a_directory_at_the_guard_path_is_unsafe(guards: ActiveGuardManager) -> None:
    (guards.guard_dir / guard_artifact_name(REPO)).mkdir()
    with pytest.raises(BridgeError) as exc:
        guards.read(REPO)
    assert exc.value.code == "WORKDIR_RECOVERY_REQUIRED"
    assert "unsafe" in str(exc.value)


def test_remove_requires_the_matching_task(guards: ActiveGuardManager) -> None:
    create(guards, REPO, "agt_owner")
    with pytest.raises(BridgeError) as exc:
        guards.remove(lease_id=REPO, task_id="agt_intruder")
    assert "another task" in str(exc.value)
    assert (guards.guard_dir / guard_artifact_name(REPO)).is_file()
    guards.remove(lease_id=REPO, task_id="agt_owner")
    assert guards.read(REPO) is None

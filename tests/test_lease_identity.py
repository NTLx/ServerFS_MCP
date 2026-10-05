"""The lease-identity contract on the ServerFS side (v0.11 §5.3, §5.5).

The Bridge derives the same artifact names in its own package, because the two must not import each
other (§23). These vectors are therefore duplicated verbatim in ``agent_bridge/tests/
test_lease_identity.py``: agreement is pinned by the shared table, not by a shared import.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp import lease_identity
from serverfs_mcp.errors import AgentLeaseError
from serverfs_mcp.workdirs import Workdir

# (lease id, artifact stem) — the stem plus ".lock" is the lock artifact and the bare stem is the
# recovery guard. Slot leases keep the legacy two-digit names; every alias lease is hashed.
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
def test_lock_artifact_name(lease_id: str, stem: str) -> None:
    assert lease_identity.lock_artifact_name(lease_id) == f"{stem}.lock"


@pytest.mark.parametrize(("lease_id", "stem"), VECTORS)
def test_guard_artifact_name(lease_id: str, stem: str) -> None:
    assert lease_identity.guard_artifact_name(lease_id) == stem


def test_legacy_slot_layout_is_unchanged() -> None:
    """A v0.10 Bridge's lock files and guards are still the names this reader looks for."""
    assert lease_identity.lock_artifact_name(lease_identity.slot_lease_id(1)) == "01.lock"
    assert lease_identity.guard_artifact_name(lease_identity.slot_lease_id(16)) == "16"


def test_case_only_aliases_do_not_share_an_artifact() -> None:
    """NTFS case folding must not merge two ServerFS workdirs into one lease (§5.3)."""
    upper = lease_identity.lock_artifact_name(lease_identity.alias_lease_id("Foo"))
    lower = lease_identity.lock_artifact_name(lease_identity.alias_lease_id("foo"))
    assert upper != lower
    assert upper.casefold() != lower.casefold()


@pytest.mark.parametrize("alias", ["con", "nul", "com1", "." * 2, "a b", "x" * 63, "中", "中文"])
def test_alias_derived_names_are_filesystem_safe_and_bounded(alias: str) -> None:
    lease_id = lease_identity.alias_lease_id(alias)
    name = lease_identity.lock_artifact_name(lease_id)
    stem = lease_identity.guard_artifact_name(lease_id)
    assert name == stem + lease_identity.LOCK_SUFFIX
    assert len(name) == lease_identity.ARTIFACT_HASH_LENGTH + len(lease_identity.LOCK_SUFFIX)
    assert all(character in "0123456789abcdef" for character in stem)
    assert alias not in name


@pytest.mark.parametrize(
    "rejected",
    [
        "",
        "repo",
        "slot:00",
        "slot:17",
        "slot:1",
        "slot:abc",
        "other:repo",
        "alias:",
        "alias:bad/name",
        "alias:bad\\name",
        "alias:control\x01char",
        "alias:" + "x" * 300,
    ],
)
def test_malformed_lease_ids_fail_closed(rejected: str) -> None:
    with pytest.raises(AgentLeaseError):
        lease_identity.validate_lease_id(rejected)
    with pytest.raises(AgentLeaseError):
        lease_identity.lock_artifact_name(rejected)


@pytest.mark.parametrize("alias", ["", 1, None, "slot:01"])
def test_alias_lease_id_rejects_a_non_alias(alias: object) -> None:
    with pytest.raises(AgentLeaseError):
        lease_identity.alias_lease_id(alias)  # type: ignore[arg-type]


@pytest.mark.parametrize("slot", [0, 17, -1, True, "1", None])
def test_slot_lease_id_is_strict(slot: object) -> None:
    with pytest.raises(AgentLeaseError):
        lease_identity.slot_lease_id(slot)  # type: ignore[arg-type]


def test_workdir_keys_a_legacy_slot_workdir_by_slot() -> None:
    workdir = Workdir(alias="repo", root=Path("."), description=None, legacy_slot=7)
    assert workdir.lease_id == "slot:07"


def test_workdir_keys_a_native_workdir_by_exact_alias() -> None:
    workdir = Workdir(alias="Repo", root=Path("."), description=None)
    assert workdir.lease_id == "alias:Repo"
    assert lease_identity.lock_artifact_name(workdir.lease_id).endswith(".lock")


def test_two_native_workdirs_differing_only_in_case_keep_separate_leases() -> None:
    upper = Workdir(alias="Repo", root=Path("."), description=None)
    lower = Workdir(alias="repo", root=Path("."), description=None)
    assert lease_identity.lock_artifact_name(upper.lease_id) != lease_identity.lock_artifact_name(
        lower.lease_id
    )

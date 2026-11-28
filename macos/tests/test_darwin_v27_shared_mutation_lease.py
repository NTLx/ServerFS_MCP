"""Private macOS Shared Core writer lease boundary, with synthetic artifacts only."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.core_bridge_client_v2 import CoreBridgeClientV2Error
from serverfs_mcp.darwin_shared_mutation_lease import SharedMutationLease
from serverfs_mcp.errors import AgentLeaseError, WorkdirBusyError, WorkdirRecoveryRequiredError
from serverfs_mcp.lease_identity import (
    alias_lease_id,
    guard_artifact_name,
    lock_artifact_name,
    writer_lease_proof,
)


def _namespace(root: Path, lease_id: str) -> Path:
    root.mkdir(mode=0o700)
    (root / "active").mkdir(mode=0o700)
    (root / lock_artifact_name(lease_id)).touch(mode=0o600)
    return root


def test_shared_writer_proof_is_held_on_exact_owner_inode(tmp_path: Path) -> None:
    lease_id = alias_lease_id("Repo")
    root = _namespace(tmp_path.resolve() / "owner", lease_id)
    work = tmp_path.resolve() / "work"
    work.mkdir()
    alternate = _namespace(tmp_path.resolve() / "other-owner", lease_id)
    seen = []

    def verify(proof: str) -> None:
        seen.append(proof)
        if proof != writer_lease_proof(root, lease_id, work):
            raise CoreBridgeClientV2Error("wrong proof")
        with (root / lock_artifact_name(lease_id)).open("rb") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    client = SimpleNamespace(verify_writer_lease=verify)
    selected = SharedMutationLease(root, {lease_id: client})
    other = SharedMutationLease(alternate, {lease_id: client})
    with selected.hold(lease_id, work):
        assert len(seen) == 1
        with pytest.raises(WorkdirBusyError):
            with selected.hold(lease_id, work):
                pytest.fail("second Core acquired the same lock")
    with pytest.raises(AgentLeaseError):
        with other.hold(lease_id, work):
            pytest.fail("substitute namespace accepted")
    wrong_root = tmp_path.resolve() / "other-work"
    wrong_root.mkdir()
    with pytest.raises(AgentLeaseError):
        with selected.hold(lease_id, wrong_root):
            pytest.fail("wrong physical workdir accepted")
    with selected.hold(lease_id, work):
        pass
    assert len(seen) == 4


def test_shared_writer_refuses_missing_unverified_and_recovery_state(tmp_path: Path) -> None:
    lease_id = alias_lease_id("Repo")
    root = _namespace(tmp_path.resolve() / "owner", lease_id)
    work = tmp_path.resolve() / "work"
    work.mkdir()
    client = SimpleNamespace(verify_writer_lease=lambda _: None)
    selected = SharedMutationLease(root, {lease_id: client})
    guard = root / "active" / guard_artifact_name(lease_id)
    guard.touch(mode=0o600)
    with pytest.raises(WorkdirRecoveryRequiredError):
        with selected.hold(lease_id, work):
            pytest.fail("persistent recovery bypassed")
    guard.unlink()
    for forbidden in ("alias:Other", "slot:01"):
        with pytest.raises(AgentLeaseError):
            with selected.hold(forbidden, work):
                pytest.fail("ungranted lease accepted")
    for candidate in (SharedMutationLease(None, {lease_id: client}), SharedMutationLease(root, {})):
        with pytest.raises(AgentLeaseError):
            with candidate.hold(lease_id, work):
                pytest.fail("missing owner identity accepted")

    def refuse(_: str) -> None:
        raise CoreBridgeClientV2Error("legacy owner does not support proof")

    with pytest.raises(AgentLeaseError):
        with SharedMutationLease(
            root, {lease_id: SimpleNamespace(verify_writer_lease=refuse)}
        ).hold(lease_id, work):
            pytest.fail("legacy response accepted as a lease proof")
    lock = root / lock_artifact_name(lease_id)
    lock.unlink()
    with pytest.raises(AgentLeaseError):
        with selected.hold(lease_id, work):
            pytest.fail("missing lock recreated")
    assert not lock.exists()


def test_proof_rejects_linked_and_unsafe_artifacts(tmp_path: Path) -> None:
    lease_id = alias_lease_id("Repo")
    root = _namespace(tmp_path.resolve() / "owner", lease_id)
    work = tmp_path.resolve() / "work"
    work.mkdir()
    lock = root / lock_artifact_name(lease_id)
    with lock.open("rb") as handle:
        os.link(lock, root / "hardlink")
        with pytest.raises(ValueError):
            writer_lease_proof(root, lease_id, work, opened=os.fstat(handle.fileno()))
        (root / "hardlink").unlink()
        lock.rename(root / "retained")
        lock.touch(mode=0o600)
        with pytest.raises(ValueError):
            writer_lease_proof(root, lease_id, work, opened=os.fstat(handle.fileno()))
    lock.chmod(0o666)
    with pytest.raises(ValueError):
        writer_lease_proof(root, lease_id, work)
    lock.unlink()
    lock.symlink_to(root / "retained")
    with pytest.raises(ValueError):
        writer_lease_proof(root, lease_id, work)

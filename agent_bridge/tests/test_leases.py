from __future__ import annotations

import os
from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.leases import LeaseManager


def test_exclusive_lease_rejects_second_holder(tmp_path: Path) -> None:
    manager = LeaseManager(tmp_path / "locks")
    first = manager.acquire_exclusive(1)
    try:
        with pytest.raises(BridgeError) as exc:
            manager.acquire_exclusive(1)
        assert exc.value.code == "WORKDIR_BUSY"
    finally:
        first.release()

    second = manager.acquire_exclusive(1)
    second.release()


def test_different_slots_do_not_conflict(tmp_path: Path) -> None:
    manager = LeaseManager(tmp_path / "locks")
    first = manager.acquire_exclusive(1)
    second = manager.acquire_exclusive(2)
    first.release()
    second.release()


def test_private_manager_precreates_private_lock_files(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    LeaseManager(lock_dir)
    assert lock_dir.stat().st_mode & 0o777 == 0o700
    files = sorted(lock_dir.glob("*.lock"))
    assert len(files) == 16
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)


def test_shared_manager_precreates_group_readable_lock_files(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    LeaseManager(lock_dir, shared_gid=os.getgid())
    assert lock_dir.stat().st_mode & 0o777 == 0o750
    assert lock_dir.stat().st_gid == os.getgid()
    files = sorted(lock_dir.glob("*.lock"))
    assert len(files) == 16
    assert all(path.stat().st_mode & 0o777 == 0o640 for path in files)
    assert all(path.stat().st_gid == os.getgid() for path in files)


def test_existing_non_private_lock_dir_fails_without_chmod(tmp_path: Path) -> None:
    lock_dir = tmp_path / "public"
    lock_dir.mkdir()
    lock_dir.chmod(0o755)
    with pytest.raises(ValueError, match="mode 0700"):
        LeaseManager(lock_dir)
    assert lock_dir.stat().st_mode & 0o777 == 0o755

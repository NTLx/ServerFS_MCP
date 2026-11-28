"""Windows writer lease — the §37 matrix for the LockFileEx backend (v0.11 Phase C).

The measurements these cases re-enforce are Phase 0A's: an exclusive ``LockFileEx`` succeeds on a
read-only handle, a conflict is returned immediately instead of blocking, the OS reclaims the lock
when the owning process dies by any means, and the lock is OS-enforced rather than advisory. The
contention cases run between **real processes**, because a lock taken twice in one process is a
different question — and that one matters too, since it is why ServerFS keeps its process-wide
mutation lock.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from platform_contract import require_windows_kernel
from serverfs_agent_bridge import windows_lease
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.lease_identity import alias_lease_id, lock_artifact_name
from serverfs_agent_bridge.leases import LeaseManager

require_windows_kernel("the writer lease backend is LockFileEx")

LEASE_ID = alias_lease_id("repo")

HOLDER = """
import os, sys
from pathlib import Path
from serverfs_agent_bridge.leases import LeaseManager

lock_dir, lease_id = sys.argv[1], sys.argv[2]
manager = LeaseManager(Path(lock_dir), lease_ids=[lease_id])
lease = manager.acquire_exclusive(lease_id)
print("HELD", flush=True)
if sys.argv[3] == "exit":
    os._exit(0)
sys.stdin.read()
"""


def start_holder(lock_dir: Path, *, mode: str = "hold") -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_dir), LEASE_ID, mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    if mode == "exit":
        return process
    line = process.stdout.readline()
    assert line.startswith("HELD"), line
    return process


def stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    process.wait(timeout=15)


def acquire_after_death(manager: LeaseManager) -> None:
    """Take the lease after its previous owner died, within a bound, or fail the measurement.

    The OS reclaims the lock when the dead process's handle table is torn down, which is
    asynchronous with the parent's ``wait()`` returning — so this asserts that it *ends*, not that
    it is instantaneous.
    """
    deadline = time.monotonic() + 5.0
    last: str | None = None
    while True:
        try:
            manager.acquire_exclusive(LEASE_ID).release()
            return
        except BridgeError as exc:
            last = exc.code
            if time.monotonic() >= deadline:
                raise AssertionError(f"the lease was never reclaimed (last code {last})") from exc
            time.sleep(0.05)


def test_cross_process_contention_is_refused_immediately(tmp_path: Path) -> None:
    """A busy lease returns at once: a blocked probe would stall the mutation path (§C3)."""
    lock_dir = tmp_path / "locks"
    manager = LeaseManager(lock_dir, lease_ids=[LEASE_ID])
    holder = start_holder(lock_dir)
    started = time.monotonic()
    try:
        with pytest.raises(BridgeError) as exc:
            manager.acquire_exclusive(LEASE_ID)
        elapsed = time.monotonic() - started
    finally:
        stop(holder)
    assert exc.value.code == "WORKDIR_BUSY"
    assert elapsed < 2.0, f"the busy acquisition took {elapsed:.3f}s"


def test_release_after_an_unclean_process_death(tmp_path: Path) -> None:
    """TerminateProcess runs no user-mode cleanup, and the OS still reclaims the lock (§C6)."""
    lock_dir = tmp_path / "locks"
    manager = LeaseManager(lock_dir, lease_ids=[LEASE_ID])
    holder = start_holder(lock_dir)
    holder.kill()
    holder.wait(timeout=15)
    acquire_after_death(manager)


def test_release_after_os_exit(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    start_holder(lock_dir, mode="exit").wait(timeout=15)
    acquire_after_death(LeaseManager(lock_dir, lease_ids=[LEASE_ID]))


def test_release_on_ordinary_close(tmp_path: Path) -> None:
    manager = LeaseManager(tmp_path / "locks", lease_ids=[LEASE_ID])
    first = manager.acquire_exclusive(LEASE_ID)
    first.release()
    manager.acquire_exclusive(LEASE_ID).release()


def test_lock_is_os_enforced_for_content_reads(tmp_path: Path) -> None:
    """The lock is not advisory (§3.5): a foreign process cannot read the held artifact's content.

    The open itself still succeeds, which is what lets a diagnostic read the object's metadata — the
    bytes behind a locked range are what are refused.
    """
    lock_dir = tmp_path / "locks"
    LeaseManager(lock_dir, lease_ids=[LEASE_ID])
    holder = start_holder(lock_dir)
    try:
        with (lock_dir / lock_artifact_name(LEASE_ID)).open("rb") as handle:
            with pytest.raises(OSError):
                handle.read()
    finally:
        stop(holder)


def test_second_independent_handle_in_one_process_conflicts(tmp_path: Path) -> None:
    """Matches ``flock``, and is why ServerFS keeps its process-wide mutation lock (§C5)."""
    manager = LeaseManager(tmp_path / "locks", lease_ids=[LEASE_ID])
    first = manager.acquire_exclusive(LEASE_ID)
    with pytest.raises(BridgeError) as exc:
        manager.acquire_exclusive(LEASE_ID)
    assert exc.value.code == "WORKDIR_BUSY"
    first.release()


def test_hardlinked_name_is_the_same_lease(tmp_path: Path) -> None:
    """Lease identity is the file object, not the name (§3.6)."""
    lock_dir = tmp_path / "locks"
    manager = LeaseManager(lock_dir, lease_ids=[LEASE_ID])
    artifact = lock_dir / lock_artifact_name(LEASE_ID)
    twin = lock_dir / "twin.lock"
    try:
        twin.hardlink_to(artifact)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable here: {exc}")
    held = manager.acquire_exclusive(LEASE_ID)
    with pytest.raises(BridgeError) as exc:
        windows_lease.acquire_exclusive(twin, LEASE_ID)
    assert exc.value.code == "WORKDIR_BUSY"
    held.release()


def test_a_directory_at_the_artifact_path_is_refused(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    manager = LeaseManager(lock_dir, lease_ids=[])
    (lock_dir / lock_artifact_name(LEASE_ID)).mkdir()
    with pytest.raises(BridgeError) as exc:
        manager.acquire_exclusive(LEASE_ID)
    assert exc.value.code == "LOCK_PATH_UNSAFE"


def test_an_absent_artifact_is_refused_and_never_created(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    manager = LeaseManager(lock_dir, lease_ids=[])
    with pytest.raises(BridgeError) as exc:
        manager.acquire_exclusive(LEASE_ID)
    assert exc.value.code == "LOCK_PATH_UNSAFE"
    assert not (lock_dir / lock_artifact_name(LEASE_ID)).exists()

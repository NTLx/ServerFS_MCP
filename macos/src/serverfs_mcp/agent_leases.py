"""macOS Core reader of Bridge-owned writer leases and persistent recovery guards.

Core opens existing lease artifacts read-only and never creates them.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from .errors import AgentLeaseError, WorkdirBusyError, WorkdirRecoveryRequiredError
from .lease_identity import guard_artifact_name, lock_artifact_name, validate_lease_id


@contextlib.contextmanager
def mutation_agent_lease(lock_dir: Path, lease_id: str, *, enabled: bool) -> Iterator[None]:
    """Take the same per-workdir exclusive lock used by workspace-write Agent tasks.

    When Agent Bridge integration is disabled this is a no-op, preserving the v0.2 mutation
    contract. When enabled, the artifact must already exist and is opened read-only; ServerFS never
    creates or modifies it.
    """
    if not enabled:
        yield
        return
    lease_id = validate_lease_id(lease_id)
    if not lock_dir.is_absolute():
        raise AgentLeaseError("shared Agent lock directory is invalid")

    hold = _flock_hold(lock_dir, lease_id)
    with hold:
        # A live workspace-write task holds both the lock and the persistent guard. Only an
        # available lock plus a remaining guard is recovery state, so the lock is taken first.
        _refuse_unrecovered_workdir(lock_dir, lease_id)
        yield


@contextlib.contextmanager
def _flock_hold(lock_dir: Path, lease_id: str) -> Iterator[None]:
    path = lock_dir / lock_artifact_name(lease_id)
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AgentLeaseError from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AgentLeaseError("shared Agent lock path is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkdirBusyError from exc
        yield
    finally:
        os.close(fd)


def _refuse_unrecovered_workdir(lock_dir: Path, lease_id: str) -> None:
    guard_path = lock_dir / "active" / guard_artifact_name(lease_id)
    state = _posix_guard_state(guard_path)
    if state is None:
        return
    if not state:
        raise AgentLeaseError("shared Agent recovery guard is unsafe")
    raise WorkdirRecoveryRequiredError


def _posix_guard_state(guard_path: Path) -> bool | None:
    try:
        guard_stat = os.lstat(guard_path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AgentLeaseError("shared Agent recovery guard is unavailable") from exc
    return bool(stat.S_ISREG(guard_stat.st_mode))

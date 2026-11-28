"""Windows Core reader of Bridge-owned writer leases and persistent recovery guards.

Core opens existing lease artifacts read-only and never creates them.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from .errors import AgentLeaseError, WorkdirRecoveryRequiredError
from .lease_identity import guard_artifact_name, validate_lease_id


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

    hold = _windows_hold(lock_dir, lease_id)
    with hold:
        # A live workspace-write task holds both the lock and the persistent guard. Only an
        # available lock plus a remaining guard is recovery state, so the lock is taken first.
        _refuse_unrecovered_workdir(lock_dir, lease_id)
        yield


@contextlib.contextmanager
def _windows_hold(lock_dir: Path, lease_id: str) -> Iterator[None]:
    from . import windows_lease

    with windows_lease.hold(lock_dir, lease_id):
        yield


def _refuse_unrecovered_workdir(lock_dir: Path, lease_id: str) -> None:
    guard_path = lock_dir / "active" / guard_artifact_name(lease_id)
    from . import windows_lease

    state = windows_lease.guard_state(guard_path)
    if state is None:
        return
    if not state:
        raise AgentLeaseError("shared Agent recovery guard is unsafe")
    raise WorkdirRecoveryRequiredError

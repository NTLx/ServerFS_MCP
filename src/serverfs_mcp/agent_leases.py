"""Container-side reader of Bridge-owned cross-process workdir lease files."""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
from collections.abc import Iterator
from pathlib import Path


class AgentLeaseError(Exception):
    code = "AGENT_LOCK_UNAVAILABLE"
    message = "shared Agent workdir lease is unavailable"


class WorkdirBusyError(AgentLeaseError):
    code = "WORKDIR_BUSY"
    message = "workdir is busy with an active Agent task"


@contextlib.contextmanager
def mutation_agent_lease(lock_dir: Path, slot: int, *, enabled: bool) -> Iterator[None]:
    """Take the same per-slot flock used by workspace-write Agent tasks.

    When Agent Bridge integration is disabled this is a no-op, preserving the
    v0.2 mutation contract. When enabled, lock files must already exist and
    are opened read-only; the container never creates or modifies host lock
    files.
    """
    if not enabled:
        yield
        return
    if type(slot) is not int or not 1 <= slot <= 16:
        raise AgentLeaseError("invalid workdir slot")
    if not lock_dir.is_absolute():
        raise AgentLeaseError("shared Agent lock directory is invalid")

    path = lock_dir / f"{slot:02d}.lock"
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
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

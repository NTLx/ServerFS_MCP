"""macOS cross-process workdir leases; retain the existing lease identities and artifact names."""

from __future__ import annotations

import fcntl
import os
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import lease_identity, private_state
from .errors import BridgeError
from .lease_identity import lock_artifact_name, slot_lease_id, validate_lease_id


class LeaseHandle(Protocol):
    """A platform-local lease handle that releases itself."""

    path: Path

    def release(self) -> None: ...


@dataclass
class WorkdirLease:
    fd: int
    path: Path

    def release(self) -> None:
        if self.fd < 0:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> WorkdirLease:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class LeaseManager:
    def __init__(
        self,
        lock_dir: Path,
        *,
        shared_gid: int | None = None,
        lease_ids: Iterable[str] = (),
    ):
        # Only the Bridge creates lock artifacts; Core opens existing ones.
        self.lock_dir = lock_dir
        if shared_gid is not None and (type(shared_gid) is not int or shared_gid < 0):
            raise ValueError("shared_gid must be a non-negative integer")
        self.shared_gid = shared_gid
        self.lease_ids = tuple(validate_lease_id(lease_id) for lease_id in lease_ids)
        directory_mode = 0o750 if shared_gid is not None else 0o700
        file_mode = 0o640 if shared_gid is not None else 0o600

        private_state.ensure_private_directory(
            self.lock_dir,
            mode=directory_mode,
            shared_gid=shared_gid,
            parents=True,
            messages=private_state.DirectoryMessages(
                not_a_directory="lock_dir must be a real directory",
                not_owned="lock_dir must be owned by the bridge user",
                private_mode="private lock_dir must be mode 0700",
                shared_mode="shared lock_dir must not be group-writable or world-accessible",
                group_change_failed="lock_dir group cannot be set to shared_gid",
            ),
        )
        self._prepare_lock_files(file_mode)

    def _prepare_lock_files(self, mode: int) -> None:
        # The legacy layout is preserved exactly, including the slots that no workdir is
        # configured for: a running deployment's artifact names must not change on upgrade.
        for slot in range(1, lease_identity.MAX_WORKDIR_SLOTS + 1):
            self._prepare_one(slot_lease_id(slot), mode)
        for lease_id in self.lease_ids:
            self._prepare_one(lease_id, mode)

    def _prepare_one(self, lease_id: str, mode: int) -> None:
        path = self.lock_dir / lock_artifact_name(lease_id)
        try:
            fd = os.open(
                path,
                private_state.open_flags("read-write", create=True),
                mode,
            )
        except OSError as exc:
            raise ValueError("workdir lease file cannot be prepared") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("workdir lease path must be a regular file")
            if self.shared_gid is not None and st.st_gid != self.shared_gid:
                try:
                    os.fchown(fd, -1, self.shared_gid)
                except OSError as exc:
                    raise ValueError("workdir lease group cannot be set") from exc
            os.fchmod(fd, mode)
        finally:
            os.close(fd)

    def acquire_exclusive(self, lease_id: str) -> LeaseHandle:
        validate_lease_id(lease_id)
        path = self.lock_dir / lock_artifact_name(lease_id)
        return self._acquire_flock(lease_id, path)

    def _acquire_flock(self, lease_id: str, path: Path) -> WorkdirLease:
        if sys.platform != "darwin":
            raise BridgeError(
                "BRIDGE_PLATFORM_UNSUPPORTED",
                "the flock-backed writer lease is not available in this Bridge process",
            )
        try:
            fd = os.open(
                path,
                private_state.open_flags("read-write"),
            )
        except OSError as exc:
            raise BridgeError("LOCK_PATH_UNSAFE", "workdir lease path is unavailable") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise BridgeError("LOCK_PATH_UNSAFE", "workdir lease path is invalid")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise BridgeError(
                "WORKDIR_BUSY", f"{lease_identity.describe(lease_id)} is busy"
            ) from exc
        except Exception:
            os.close(fd)
            raise
        return WorkdirLease(fd=fd, path=path)

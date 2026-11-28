"""Windows cross-process workdir leases; retain the existing lease identities and artifact names."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from . import private_state
from .lease_identity import lock_artifact_name, validate_lease_id


class LeaseHandle(Protocol):
    """A platform-local lease handle that releases itself."""

    path: Path

    def release(self) -> None: ...


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
        # Preserve the native alias-keyed artifact layout.
        for lease_id in self.lease_ids:
            self._prepare_one(lease_id, mode)

    def _prepare_one(self, lease_id: str, mode: int) -> None:
        path = self.lock_dir / lock_artifact_name(lease_id)
        # Explicit DACL on every artifact; verify existing files rather than repair.
        private_state.ensure_private_file(
            path, mode=mode, not_regular="workdir lease path must be a regular file"
        )

    def acquire_exclusive(self, lease_id: str) -> LeaseHandle:
        validate_lease_id(lease_id)
        path = self.lock_dir / lock_artifact_name(lease_id)
        from . import windows_lease

        return windows_lease.acquire_exclusive(path, lease_id)

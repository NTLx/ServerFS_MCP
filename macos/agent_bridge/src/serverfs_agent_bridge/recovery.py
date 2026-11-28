"""Persistent recovery guards keyed by the same lease identity as the leases they sit on."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import lease_identity, private_state
from .errors import BridgeError
from .lease_identity import (
    guard_artifact_name,
    is_guard_artifact_name,
    slot_lease_id,
    validate_lease_id,
)
from .util import utc_now


@dataclass(frozen=True)
class ActiveGuard:
    lease_id: str
    payload: dict[str, Any]


class ActiveGuardManager:
    def __init__(self, lock_dir: Path, *, shared_gid: int | None = None):
        self.guard_dir = lock_dir / "active"
        directory_mode = 0o750 if shared_gid is not None else 0o700
        file_mode = 0o640 if shared_gid is not None else 0o600
        self.file_mode = file_mode
        self.shared_gid = shared_gid
        private_state.ensure_private_directory(
            self.guard_dir,
            mode=directory_mode,
            shared_gid=shared_gid,
            messages=private_state.DirectoryMessages(
                not_a_directory="active guard directory must be a real directory",
                not_owned="active guard directory must be owned by the bridge user",
                private_mode="private active guard directory must be mode 0700",
                shared_mode="shared active guard directory must not be group-writable or public",
            ),
        )

    def create(
        self,
        *,
        lease_id: str,
        task_id: str,
        runtime: str,
        workdir_alias: str,
        correlation_id: str | None,
    ) -> None:
        lease_id = validate_lease_id(lease_id)
        path = self._path(lease_id)
        if path.exists() or path.is_symlink():
            raise BridgeError(
                "WORKDIR_RECOVERY_REQUIRED",
                f"{lease_identity.describe(lease_id)} has unresolved Agent recovery state",
            )
        slot = lease_identity.slot_of(lease_id)
        payload = {
            "schema_version": 1,
            "lease_id": lease_id,
            "slot": slot,
            "task_id": task_id,
            "runtime": runtime,
            "workdir_alias": workdir_alias,
            "correlation_id": correlation_id,
            "native_session_id": None,
            "native_turn_id": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        self._publish(path, payload, create_only=True)

    def update_native_ids(
        self,
        *,
        lease_id: str,
        task_id: str,
        native_session_id: str | None,
        native_turn_id: str | None,
    ) -> None:
        guard = self.read(lease_id)
        if guard is None or guard.payload.get("task_id") != task_id:
            raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard does not match task")
        payload = dict(guard.payload)
        if native_session_id is not None:
            payload["native_session_id"] = native_session_id
        if native_turn_id is not None:
            payload["native_turn_id"] = native_turn_id
        payload["updated_at"] = utc_now()
        self._publish(self._path(lease_id), payload, create_only=False)

    def read(self, lease_id: str) -> ActiveGuard | None:
        lease_id = validate_lease_id(lease_id)
        return self._read_at(self._path(lease_id), lease_id)

    def _read_at(self, path: Path, expected_lease_id: str | None) -> ActiveGuard | None:
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return None
        if not private_state.state_entry_is_regular(file_stat):
            raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard path is unsafe")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard is unreadable") from exc
        lease_id = self._identity_of(path, data)
        if expected_lease_id is not None and lease_id != expected_lease_id:
            raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard is invalid")
        return ActiveGuard(lease_id=lease_id, payload=data)

    def _identity_of(self, path: Path, data: Any) -> str:
        """The lease this guard belongs to, verified against the artifact name it was found under.

        A guard payload cannot claim a different workdir than the path it occupies: the name is
        derived from the lease id, so checking the pair closes the door on a planted or moved file.
        A v0.10 payload has no ``lease_id`` and names only its slot, which is still a valid lease id
        of the legacy kind — upgrade must not render an in-flight guard unreadable and therefore
        unclearable.
        """
        invalid = BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard is invalid")
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise invalid
        if not isinstance(data.get("task_id"), str) or not isinstance(data.get("runtime"), str):
            raise invalid
        recorded = data.get("lease_id")
        if recorded is None:
            slot = data.get("slot")
            if type(slot) is not int:
                raise invalid
            try:
                lease_id = slot_lease_id(slot)
            except lease_identity.LeaseIdentityError as exc:
                raise invalid from exc
        else:
            if not isinstance(recorded, str):
                raise invalid
            try:
                lease_id = validate_lease_id(recorded)
            except lease_identity.LeaseIdentityError as exc:
                raise invalid from exc
            recorded_slot = data.get("slot")
            if recorded_slot is not None and recorded_slot != lease_identity.slot_of(lease_id):
                raise invalid
        if guard_artifact_name(lease_id) != path.name:
            raise invalid
        return lease_id

    def list(self) -> list[ActiveGuard]:
        """Every guard present, by scanning the directory.

        Alias-derived artifacts cannot be enumerated by slot any more, and an unreadable guard is a
        recovery condition rather than something to skip: skipping it would let a workdir look
        available while it is still owned.
        """
        guards: list[ActiveGuard] = []
        try:
            entries = sorted(self.guard_dir.iterdir(), key=lambda entry: entry.name)
        except FileNotFoundError:
            return guards
        for entry in entries:
            if not is_guard_artifact_name(entry.name):
                continue
            guard = self._read_at(entry, None)
            if guard is not None:
                guards.append(guard)
        return guards

    def remove(self, *, lease_id: str, task_id: str) -> None:
        guard = self.read(lease_id)
        if guard is None:
            return
        if guard.payload.get("task_id") != task_id:
            raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard belongs to another task")
        try:
            self._path(lease_id).unlink()
        except OSError as exc:
            raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "could not clear active guard") from exc

    def _publish(self, path: Path, payload: dict[str, Any], *, create_only: bool) -> None:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        tmp = self.guard_dir / f".{path.name}.{secrets.token_hex(8)}.tmp"
        flags = private_state.open_flags("write", create=True, exclusive=True)
        fd = -1
        try:
            fd = os.open(tmp, flags, self.file_mode)
            os.write(fd, encoded)
            os.fsync(fd)
            private_state.apply_private_file_mode(
                fd, mode=self.file_mode, shared_gid=self.shared_gid
            )
            os.close(fd)
            fd = -1
            if create_only and (path.exists() or path.is_symlink()):
                raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard already exists")
            os.replace(tmp, path)
        except BridgeError:
            raise
        except OSError as exc:
            raise BridgeError(
                "WORKDIR_RECOVERY_REQUIRED",
                "could not persist active guard",
            ) from exc
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _path(self, lease_id: str) -> Path:
        return self.guard_dir / guard_artifact_name(validate_lease_id(lease_id))

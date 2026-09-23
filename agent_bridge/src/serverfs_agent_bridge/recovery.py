"""Persistent active-slot recovery guards layered on top of flock leases."""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BridgeError
from .util import utc_now


@dataclass(frozen=True)
class ActiveGuard:
    slot: int
    payload: dict[str, Any]


class ActiveGuardManager:
    def __init__(self, lock_dir: Path, *, shared_gid: int | None = None):
        self.guard_dir = lock_dir / "active"
        directory_mode = 0o750 if shared_gid is not None else 0o700
        file_mode = 0o640 if shared_gid is not None else 0o600
        self.file_mode = file_mode
        self.shared_gid = shared_gid
        try:
            directory_stat = self.guard_dir.lstat()
        except FileNotFoundError:
            self.guard_dir.mkdir(mode=directory_mode)
            directory_stat = self.guard_dir.lstat()
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            raise ValueError("active guard directory must be a real directory")
        if directory_stat.st_uid != os.getuid():
            raise ValueError("active guard directory must be owned by the bridge user")
        if shared_gid is None:
            if directory_stat.st_mode & 0o077:
                raise ValueError("private active guard directory must be mode 0700")
        else:
            if directory_stat.st_mode & 0o027:
                raise ValueError(
                    "shared active guard directory must not be group-writable or public"
                )
            if directory_stat.st_gid != shared_gid:
                os.chown(self.guard_dir, -1, shared_gid)
        os.chmod(self.guard_dir, directory_mode)

    def create(
        self,
        *,
        slot: int,
        task_id: str,
        runtime: str,
        workdir_alias: str,
        correlation_id: str | None,
    ) -> None:
        path = self._path(slot)
        if path.exists() or path.is_symlink():
            raise BridgeError(
                "WORKDIR_RECOVERY_REQUIRED",
                f"workdir slot {slot:02d} has unresolved Agent recovery state",
            )
        payload = {
            "schema_version": 1,
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
        slot: int,
        task_id: str,
        native_session_id: str | None,
        native_turn_id: str | None,
    ) -> None:
        guard = self.read(slot)
        if guard is None or guard.payload.get("task_id") != task_id:
            raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard does not match task")
        payload = dict(guard.payload)
        if native_session_id is not None:
            payload["native_session_id"] = native_session_id
        if native_turn_id is not None:
            payload["native_turn_id"] = native_turn_id
        payload["updated_at"] = utc_now()
        self._publish(self._path(slot), payload, create_only=False)

    def read(self, slot: int) -> ActiveGuard | None:
        path = self._path(slot)
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard path is unsafe")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard is unreadable") from exc
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != 1
            or data.get("slot") != slot
            or not isinstance(data.get("task_id"), str)
            or not isinstance(data.get("runtime"), str)
        ):
            raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard is invalid")
        return ActiveGuard(slot=slot, payload=data)

    def list(self) -> list[ActiveGuard]:
        guards: list[ActiveGuard] = []
        for slot in range(1, 17):
            guard = self.read(slot)
            if guard is not None:
                guards.append(guard)
        return guards

    def remove(self, *, slot: int, task_id: str) -> None:
        guard = self.read(slot)
        if guard is None:
            return
        if guard.payload.get("task_id") != task_id:
            raise BridgeError("WORKDIR_RECOVERY_REQUIRED", "active guard belongs to another task")
        try:
            self._path(slot).unlink()
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
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        try:
            fd = os.open(tmp, flags, self.file_mode)
            os.write(fd, encoded)
            os.fsync(fd)
            os.fchmod(fd, self.file_mode)
            if self.shared_gid is not None:
                os.fchown(fd, -1, self.shared_gid)
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

    def _path(self, slot: int) -> Path:
        if type(slot) is not int or not 1 <= slot <= 16:
            raise BridgeError("INVALID_WORKDIR_SLOT", "workdir slot must be between 1 and 16")
        return self.guard_dir / f"{slot:02d}"

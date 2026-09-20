"""Cross-process workdir leases backed by Linux flock."""

from __future__ import annotations

import fcntl
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import BridgeError


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
    def __init__(self, lock_dir: Path, *, shared_gid: int | None = None):
        self.lock_dir = lock_dir
        if shared_gid is not None and (type(shared_gid) is not int or shared_gid < 0):
            raise ValueError("shared_gid must be a non-negative integer")
        self.shared_gid = shared_gid
        directory_mode = 0o750 if shared_gid is not None else 0o700
        file_mode = 0o640 if shared_gid is not None else 0o600

        try:
            lock_stat = self.lock_dir.lstat()
        except FileNotFoundError:
            self.lock_dir.mkdir(parents=True, exist_ok=True, mode=directory_mode)
            lock_stat = self.lock_dir.lstat()
        if stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISDIR(lock_stat.st_mode):
            raise ValueError("lock_dir must be a real directory")
        if lock_stat.st_uid != os.getuid():
            raise ValueError("lock_dir must be owned by the bridge user")
        if shared_gid is None:
            if lock_stat.st_mode & 0o077:
                raise ValueError("private lock_dir must be mode 0700")
        else:
            if lock_stat.st_mode & 0o027:
                raise ValueError("shared lock_dir must not be group-writable or world-accessible")
            if lock_stat.st_gid != shared_gid:
                try:
                    os.chown(self.lock_dir, -1, shared_gid)
                except OSError as exc:
                    raise ValueError("lock_dir group cannot be set to shared_gid") from exc
        os.chmod(self.lock_dir, directory_mode)
        self._prepare_lock_files(file_mode)

    def _prepare_lock_files(self, mode: int) -> None:
        for slot in range(1, 17):
            path = self.lock_dir / f"{slot:02d}.lock"
            try:
                fd = os.open(
                    path,
                    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
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

    def acquire_exclusive(self, slot: int) -> WorkdirLease:
        if type(slot) is not int or not 1 <= slot <= 16:
            raise BridgeError("INVALID_WORKDIR_SLOT", "workdir slot must be between 1 and 16")
        path = self.lock_dir / f"{slot:02d}.lock"
        try:
            fd = os.open(
                path,
                os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
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
            raise BridgeError("WORKDIR_BUSY", f"workdir slot {slot:02d} is busy") from exc
        except Exception:
            os.close(fd)
            raise
        return WorkdirLease(fd=fd, path=path)

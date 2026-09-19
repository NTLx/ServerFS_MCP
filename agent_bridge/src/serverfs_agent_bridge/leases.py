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
    def __init__(self, lock_dir: Path):
        self.lock_dir = lock_dir
        try:
            lock_stat = self.lock_dir.lstat()
        except FileNotFoundError:
            self.lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_stat = self.lock_dir.lstat()
        if stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISDIR(lock_stat.st_mode):
            raise ValueError("lock_dir must be a real directory")
        if lock_stat.st_uid != os.getuid() or lock_stat.st_mode & 0o077:
            raise ValueError("lock_dir must be owned by the bridge user and mode 0700")
        os.chmod(self.lock_dir, 0o700)

    def acquire_exclusive(self, slot: int) -> WorkdirLease:
        if type(slot) is not int or not 1 <= slot <= 16:
            raise BridgeError("INVALID_WORKDIR_SLOT", "workdir slot must be between 1 and 16")
        path = self.lock_dir / f"{slot:02d}.lock"
        try:
            fd = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise BridgeError("LOCK_PATH_UNSAFE", "workdir lease path is unavailable") from exc
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise BridgeError("WORKDIR_BUSY", f"workdir slot {slot:02d} is busy") from exc
        except Exception:
            os.close(fd)
            raise
        return WorkdirLease(fd=fd, path=path)

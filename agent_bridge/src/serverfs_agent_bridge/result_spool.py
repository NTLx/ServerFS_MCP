"""Private final-response spool for oversized Agent results."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import BridgeError


@dataclass(frozen=True)
class ResultMetadata:
    size_bytes: int
    sha256: str


class ResultSpool:
    def __init__(self, state_dir: Path):
        self.results_dir = state_dir / "results"
        try:
            directory_stat = self.results_dir.lstat()
        except FileNotFoundError:
            self.results_dir.mkdir(mode=0o700)
            directory_stat = self.results_dir.lstat()
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            raise ValueError("results_dir must be a real directory")
        if directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
            raise ValueError("results_dir must be owned by the bridge user and mode 0700")
        os.chmod(self.results_dir, 0o700)

    def write(self, task_id: str, text: str) -> ResultMetadata:
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        final_path = self._path(task_id)
        tmp_path = self.results_dir / f".{task_id}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        try:
            fd = os.open(tmp_path, flags, 0o600)
            view = memoryview(data)
            written = 0
            while written < len(view):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise OSError("short write")
                written += count
            os.fsync(fd)
            os.fchmod(fd, 0o600)
            os.close(fd)
            fd = -1
            if final_path.exists() or final_path.is_symlink():
                raise BridgeError(
                    "AGENT_RESULT_STORAGE_ERROR",
                    "result spool path already exists",
                )
            os.replace(tmp_path, final_path)
            os.chmod(final_path, 0o600)
        except BridgeError:
            raise
        except OSError as exc:
            raise BridgeError(
                "AGENT_RESULT_STORAGE_ERROR",
                "could not persist agent result",
            ) from exc
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        return ResultMetadata(size_bytes=len(data), sha256=digest)

    def read_chunk(
        self,
        task_id: str,
        *,
        offset_bytes: int,
        max_bytes: int,
    ) -> dict[str, object]:
        if type(offset_bytes) is not int or offset_bytes < 0:
            raise BridgeError("INVALID_RESULT_OFFSET", "offset_bytes must be non-negative")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 65_536:
            raise BridgeError("INVALID_RESULT_LIMIT", "max_bytes must be between 1 and 65536")

        path = self._path(task_id)
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise BridgeError("AGENT_RESULT_NOT_RETRIEVABLE", "task result is not spooled") from exc
        except OSError as exc:
            raise BridgeError("AGENT_RESULT_STORAGE_ERROR", "result spool is unavailable") from exc

        try:
            file_stat = os.fstat(fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.getuid()
                or file_stat.st_mode & 0o077
            ):
                raise BridgeError("AGENT_RESULT_STORAGE_ERROR", "result spool file is unsafe")
            size = int(file_stat.st_size)
            if offset_bytes > size:
                raise BridgeError("INVALID_RESULT_OFFSET", "offset_bytes exceeds result size")
            os.lseek(fd, offset_bytes, os.SEEK_SET)
            data = os.read(fd, min(max_bytes, size - offset_bytes))
        finally:
            os.close(fd)

        if data:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data" and exc.end == len(data):
                    data = data[: exc.start]
                    if not data:
                        raise BridgeError(
                            "INVALID_RESULT_LIMIT",
                            "max_bytes is too small for the next UTF-8 character",
                        ) from exc
                    text = data.decode("utf-8")
                else:
                    raise BridgeError(
                        "INVALID_RESULT_OFFSET",
                        "offset_bytes is not on a UTF-8 character boundary",
                    ) from exc
        else:
            text = ""

        next_offset = offset_bytes + len(data)
        return {
            "text": text,
            "offset_bytes": offset_bytes,
            "next_offset_bytes": next_offset,
            "eof": next_offset >= size,
        }

    def delete(self, task_id: str) -> None:
        path = self._path(task_id)
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise BridgeError("AGENT_RESULT_STORAGE_ERROR", "result spool file is unsafe")
        try:
            path.unlink()
        except OSError as exc:
            raise BridgeError(
                "AGENT_RESULT_STORAGE_ERROR",
                "could not delete agent result",
            ) from exc

    def _path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not task_id.startswith("agt_") or "/" in task_id:
            raise BridgeError("INVALID_REQUEST", "invalid task_id")
        return self.results_dir / f"{task_id}.txt"


def utf8_prefix(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    prefix = data[:max_bytes]
    while prefix:
        try:
            return prefix.decode("utf-8")
        except UnicodeDecodeError as exc:
            prefix = prefix[: exc.start]
    return ""

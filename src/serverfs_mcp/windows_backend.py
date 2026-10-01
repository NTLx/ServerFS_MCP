"""Windows native filesystem backend over the Rust kernel (v0.10 Phase B).

This module is the Windows counterpart of ``linux_backend.py``: it is the
only place in the MCP product path that imports ``serverfs_windows_native``,
and it is imported lazily by ``backends.get_backend`` only on ``win32``.

Lifecycle contract (§10.1, frozen by the Phase A closure review): unlike
the Linux session — which re-opens the root FD per operation exactly as
v0.9 did — the Windows backend acquires the trusted root HANDLE once per
workdir and retains it for the process lifetime. ``open_session`` returns
the cached session; no MCP tool call ever reopens the root.

Channel status: the retained-session wiring, root validation and error
normalization are live; the read/enum/mutation channels are explicit
pending stubs until the Phase B read kernel lands, each raising
``BackendError("WINDOWS_KERNEL_PENDING", ...)`` so a half-built Windows
surface can never silently answer with wrong data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import serverfs_windows_native as native

from .backends import BackendError

if TYPE_CHECKING:
    from .models import TextEdit
    from .paths import ResolvedPath
    from .workdirs import Workdir


def _to_backend_error(exc: native.NativeSessionError) -> BackendError:
    # The native layer already normalizes to (code, agent-safe message);
    # the pair crosses unchanged and never carries a host path.
    code, message = exc.args
    return BackendError(str(code), str(message))


def _pending(channel: str) -> BackendError:
    return BackendError(
        "WINDOWS_KERNEL_PENDING",
        f"the Windows '{channel}' channel arrives with the Phase B read kernel",
    )


class WindowsWorkdirSession:
    """One retained-root session for one workdir."""

    def __init__(self, native_session: object, workdir: Workdir):
        self._native = native_session
        self._workdir = workdir

    @property
    def workdir(self) -> Workdir:
        return self._workdir

    def identity_token(self) -> str:
        """Handle-read identity of the retained root (not a session open)."""
        try:
            return self._native.object_token()
        except native.NativeSessionError as exc:
            raise _to_backend_error(exc) from None

    # ---- read channels (pending: Phase B read kernel) ----

    def stat(self, resolved: ResolvedPath):
        raise _pending("stat")

    def list(self, resolved: ResolvedPath, *, offset: int, limit: int):
        raise _pending("list")

    def find(self, resolved: ResolvedPath, *, pattern: str, limit: int, max_walk_entries: int):
        raise _pending("find")

    def search(
        self,
        resolved: ResolvedPath,
        *,
        query: str,
        glob: str | None,
        case_sensitive: bool,
        limit: int,
        timeout_seconds: float,
        max_file_bytes: int,
    ):
        raise _pending("search")

    def read_text_page(
        self,
        resolved: ResolvedPath,
        *,
        start_line: int,
        max_lines: int,
        max_read_bytes: int,
        binary_sample: int,
    ):
        raise _pending("read_text_page")

    def read_binary(self, resolved: ResolvedPath, *, max_bytes: int):
        raise _pending("read_binary")

    def validate_directory(self, resolved: ResolvedPath) -> None:
        raise _pending("validate_directory")

    # ---- mutation channels (pending: Phase D) ----

    def create_file(self, resolved: ResolvedPath, content: str, *, max_write_bytes: int):
        raise _pending("create_file")

    def replace_file(
        self,
        resolved: ResolvedPath,
        expected_revision: str,
        edits: list[TextEdit],
        *,
        max_write_bytes: int,
        max_edits_per_call: int,
    ):
        raise _pending("replace_file")

    def delete_file(self, resolved: ResolvedPath, expected_revision: str):
        raise _pending("delete_file")

    def create_binary_file(self, resolved: ResolvedPath, data: bytes, *, max_binary_bytes: int):
        raise _pending("create_binary_file")

    def replace_binary_file(
        self,
        resolved: ResolvedPath,
        data: bytes,
        expected_revision: str,
        *,
        max_binary_bytes: int,
    ):
        raise _pending("replace_binary_file")

    def create_directory(self, resolved: ResolvedPath):
        raise _pending("create_directory")

    def delete_directory(self, resolved: ResolvedPath, expected_revision: str):
        raise _pending("delete_directory")


class WindowsBackend:
    """Process-lifetime Windows kernel with retained per-workdir sessions."""

    _shared: WindowsBackend | None = None

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], WindowsWorkdirSession] = {}

    @classmethod
    def shared(cls) -> WindowsBackend:
        if cls._shared is None:
            cls._shared = WindowsBackend()
        return cls._shared

    def open_session(self, workdir: Workdir) -> WindowsWorkdirSession:
        # the native session holds root HANDLE + read-only capability, so
        # capability mode is part of session identity, not just the path
        key = (workdir.alias, str(workdir.root), workdir.read_only)
        session = self._sessions.get(key)
        if session is not None:
            return session
        try:
            native_session = native.open_workdir(str(workdir.root), workdir.read_only)
        except native.NativeSessionError as exc:
            raise _to_backend_error(exc) from None
        session = WindowsWorkdirSession(native_session, workdir)
        self._sessions[key] = session
        return session


__all__ = ["WindowsBackend", "WindowsWorkdirSession"]

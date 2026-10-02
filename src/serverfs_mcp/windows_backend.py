"""Windows native filesystem backend over the Rust kernel (v0.10 Phase B).

This module is the Windows counterpart of ``linux_backend.py``: it is the
only place in the MCP product path that imports ``serverfs_windows_native``,
and it is imported lazily by ``backends.get_backend`` only on ``win32``.

Lifecycle contract (§10.1, frozen by the Phase A closure review): unlike
the Linux session — which re-opens the root FD per operation exactly as
v0.9 did — the Windows backend acquires the trusted root HANDLE once per
workdir and retains it for the process lifetime. ``open_session`` returns
the cached session; no MCP tool call ever reopens the root.

Channel status (Phase B read kernel): stat/list/read_text_page/
read_binary/validate_directory are live against the retained root handle.
find and search stay explicit pending stubs until Phase C, and all
mutation channels until Phase D, each raising
``BackendError("WINDOWS_KERNEL_PENDING", ...)`` so a half-built Windows
surface can never silently answer with wrong data.
"""

from __future__ import annotations

import datetime as _dt
import mimetypes
from typing import TYPE_CHECKING

import serverfs_windows_native as native

from .backends import BackendError, TextPage
from .binary_payload import BinaryRead, BinaryTransferError
from .models import EntryInfo, StatFileResult

if TYPE_CHECKING:
    from .models import TextEdit
    from .paths import ResolvedPath
    from .workdirs import Workdir

# Windows FILETIME epoch offset (1601-01-01 to 1970-01-01, 100ns units).
_UNIX_EPOCH_100NS = 116_444_736_000_000_000


def _to_backend_error(exc: native.NativeSessionError) -> BackendError:
    # The native layer already normalizes to (code, agent-safe message);
    # the pair crosses unchanged and never carries a host path.
    code, message = exc.args
    return BackendError(str(code), str(message))


def _call(fn, *args):
    try:
        return fn(*args)
    except native.NativeSessionError as exc:
        raise _to_backend_error(exc) from None


def _rfc3339_from_100ns(ticks: int) -> str | None:
    if ticks <= 0:
        return None
    seconds = (ticks - _UNIX_EPOCH_100NS) / 10_000_000
    return _dt.datetime.fromtimestamp(seconds, tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pending(channel: str) -> BackendError:
    return BackendError(
        "WINDOWS_KERNEL_PENDING",
        f"the Windows '{channel}' channel arrives with a later phase",
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

    # ---- read channels (live since the Phase B read kernel) ----

    def stat(self, resolved: ResolvedPath) -> StatFileResult:
        etype, size, modified_100ns, revision = _call(self._native.stat, list(resolved.rel_parts))
        mime_type = None
        if etype == "file":
            mime_type = mimetypes.guess_type(resolved.rel_path, strict=False)[0]
            mime_type = mime_type or "application/octet-stream"
        return StatFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            type=etype,
            size=size if etype == "file" else None,
            modified_at=_rfc3339_from_100ns(modified_100ns),
            mime_type=mime_type,
            revision=revision,
        )

    def list(self, resolved: ResolvedPath, *, offset: int, limit: int):
        rows = _call(self._native.list, list(resolved.rel_parts))
        rel = resolved.rel_path
        entries: list[EntryInfo] = []
        for name, etype, size, modified_100ns in rows:
            # one filter, same policy object as every other channel
            if not resolved.allow_hidden and name.startswith("."):
                continue
            segs = (*resolved.rel_parts, name) if not rel else (*tuple(rel.split("/")), name)
            if resolved.deny_policy.is_denied(segs):
                continue
            entries.append(
                EntryInfo(
                    name=name,
                    path=f"{rel}/{name}" if rel else name,
                    type=etype,
                    size=size if etype == "file" else None,
                    modified_at=_rfc3339_from_100ns(modified_100ns),
                )
            )
        entries.sort(key=lambda x: x.name)
        has_more = offset + limit < len(entries)
        return entries[offset : offset + limit], has_more

    def read_text_page(
        self,
        resolved: ResolvedPath,
        *,
        start_line: int,
        max_lines: int,
        max_read_bytes: int,
        binary_sample: int,
    ) -> TextPage:
        revision, lines, bytes_returned, end_line, has_more, has_nul, has_bom = _call(
            self._native.read_text_page,
            list(resolved.rel_parts),
            start_line,
            max_lines,
            max_read_bytes,
            binary_sample,
        )
        return TextPage(
            revision=revision,
            lines=lines,
            bytes_returned=bytes_returned,
            end_line=end_line,
            has_more=has_more,
            has_nul=has_nul,
            has_bom=has_bom,
        )

    def read_binary(self, resolved: ResolvedPath, *, max_bytes: int) -> BinaryRead:
        try:
            data, sha256, revision = self._native.read_bounded(list(resolved.rel_parts), max_bytes)
        except native.NativeSessionError as exc:
            code, message = exc.args
            if code == "FILE_TOO_LARGE":
                raise BinaryTransferError(
                    "BINARY_FILE_TOO_LARGE",
                    f"file exceeds the {max_bytes}-byte binary transfer limit",
                ) from None
            if code == "FILE_CHANGED_DURING_READ":
                raise BinaryTransferError(
                    "FILE_CHANGED_DURING_READ", "file changed while being read; retry"
                ) from None
            raise _to_backend_error(exc) from None
        mime_type = mimetypes.guess_type(resolved.rel_path, strict=False)[0]
        return BinaryRead(
            data=data,
            size=len(data),
            mime_type=mime_type or "application/octet-stream",
            sha256=sha256,
            revision=revision,
        )

    def validate_directory(self, resolved: ResolvedPath) -> None:
        # v0.9 pre-open contract: surface PATH_NOT_FOUND / NOT_A_DIRECTORY
        # from this call, not from deep inside a walk. The retained root is
        # already validated, so only sub-paths reopen.
        parts = list(resolved.rel_parts)
        if not parts:
            return
        _call(self._native.validate_directory, parts)

    # ---- find/search (pending: Phase C native walk/search) ----

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
        self._sessions: dict[tuple[str, str, bool], WindowsWorkdirSession] = {}

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

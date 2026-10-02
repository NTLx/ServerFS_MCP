"""Windows native filesystem backend over the Rust kernel (v0.10 Phase B).

This module is the Windows counterpart of ``linux_backend.py``: it is the
only place in the MCP product path that imports ``serverfs_windows_native``,
and it is imported lazily by ``backends.get_backend`` only on ``win32``.

Lifecycle contract (§10.1, frozen by the Phase A closure review): unlike
the Linux session — which re-opens the root FD per operation exactly as
v0.9 did — the Windows backend acquires the trusted root HANDLE once per
workdir and retains it for the process lifetime. ``open_session`` returns
the cached session; no MCP tool call ever reopens the root.

Channel status (Phase B read kernel + Phase C find/search): all read,
find and search channels are live against the retained root handle; all
mutation channels stay explicit pending stubs until Phase D, each raising
``BackendError("WINDOWS_KERNEL_PENDING", ...)`` so a half-built Windows
surface can never silently answer with wrong data.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import mimetypes
import time
from typing import TYPE_CHECKING

import serverfs_windows_native as native

from .backends import BackendError, TextPage
from .binary_payload import BinaryRead, BinaryTransferError
from .models import EntryInfo, StatFileResult, TextMatch
from .paths import is_hidden_component

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


# The Linux search pins rg with `--glob !.git/!hg/!svn` regardless of
# allow_hidden (rg never reads VCS internals); the Windows searcher keeps
# that exclusion so both backends scan the same set of files.
_RG_ALWAYS_EXCLUDED_DIRS = frozenset({".git", ".hg", ".svn"})


def _glob_matches(name: str, rel_path: str, glob: str) -> bool:
    # rg --glob parity for the patterns the tool contract exposes: a
    # separator-free pattern matches the file name, anything else matches
    # the search-root-relative POSIX path
    if "/" in glob or "\\" in glob:
        return fnmatch.fnmatchcase(rel_path, glob)
    return fnmatch.fnmatchcase(name, glob)


def _scan_file(data: bytes, path: str, needle: str, case_sensitive: bool):
    for index, raw in enumerate(data.split(b"\n"), start=1):
        # rg reports whole lines with the trailing newline removed and
        # never rewrites an interior \r; non-UTF-8 bytes are lossy-decoded
        # exactly like the current text channel
        text = raw.decode("utf-8", errors="replace").rstrip("\n")
        probe = text if case_sensitive else text.casefold()
        if needle and needle in probe:
            yield TextMatch(path=path, line=index, text=text)


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

    # ---- find/search (native walk per dev_plan §16/§17: no ripgrep) ----

    def _rows(self, parts: tuple[str, ...]):
        return _call(self._native.list, list(parts))

    def find(
        self, resolved: ResolvedPath, *, pattern: str, limit: int, max_walk_entries: int
    ) -> tuple[list[str], bool]:
        # DFS over per-directory handle scans, mirroring filesystem.find_files:
        # every directory open re-validates the full component chain from the
        # retained root; entries come from the candidate+reopen verified list;
        # reparse points never match and are never descended.
        matches: list[str] = []
        visited = 0
        stack = [resolved.rel_parts]
        first = True
        while stack:
            parts = stack.pop()
            try:
                rows = self._rows(parts)
            except BackendError as exc:
                if first:
                    raise
                if exc.code in ("PATH_NOT_FOUND", "NOT_A_DIRECTORY"):
                    continue  # raced away mid-walk, like Linux
                raise
            first = False
            for name, etype, _size, _ts in sorted(rows, key=lambda r: r[0]):
                visited += 1
                if visited > max_walk_entries:
                    return matches, True
                child = (*parts, name)
                if not resolved.allow_hidden and is_hidden_component(name):
                    continue
                if resolved.deny_policy.is_denied(child):
                    continue
                if etype == "directory":
                    stack.append(child)
                elif etype == "file" and fnmatch.fnmatchcase(name, pattern):
                    matches.append("/".join(child))
                    if len(matches) >= limit:
                        return matches, True
        return matches, False

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
    ) -> tuple[list[TextMatch], bool]:
        # §17: literal fixed-string search over the policy-filtered walk.
        # Files are read through the same handle-verified bounded read; NUL
        # content is skipped whole (rg's binary suppression), oversized
        # files are skipped (rg --max-filesize), and the scan stops at
        # limit + 1 valid matches so truncation is proven, not guessed.
        deadline = time.monotonic() + timeout_seconds
        base = resolved.rel_parts
        self.validate_directory(resolved)
        needle = query if case_sensitive else query.casefold()
        matches: list[TextMatch] = []
        truncated = False
        stack = [base]
        while stack and not truncated:
            if time.monotonic() > deadline:
                raise BackendError(
                    "SEARCH_TIMEOUT", f"search exceeded the {timeout_seconds}s deadline"
                )
            parts = stack.pop()
            try:
                rows = self._rows(parts)
            except BackendError as exc:
                if exc.code in ("PATH_NOT_FOUND", "NOT_A_DIRECTORY"):
                    continue
                raise
            for name, etype, size, _ts in rows:
                child = (*parts, name)
                rel_from_root = "/".join(child[len(base) :])
                if not resolved.allow_hidden and is_hidden_component(name):
                    continue
                if resolved.deny_policy.is_denied(child):
                    continue
                if etype == "directory":
                    if name in _RG_ALWAYS_EXCLUDED_DIRS:
                        continue
                    stack.append(child)
                    continue
                if etype != "file":
                    continue
                if glob and not _glob_matches(name, rel_from_root, glob):
                    continue
                if size is None or size > max_file_bytes:
                    continue
                try:
                    data, _sha, _rev = _call(self._native.read_bounded, list(child), max_file_bytes)
                except BackendError as exc:
                    if exc.code in (
                        "FILE_TOO_LARGE",
                        "PATH_NOT_FOUND",
                        "NOT_A_FILE",
                        "REPARSE_POINT_NOT_ALLOWED",
                        "ACCESS_DENIED",
                    ):
                        continue  # raced/vanished/binary-adjacent: rg skips too
                    raise
                if b"\x00" in data:
                    continue
                for match in _scan_file(data, "/".join(child), needle, case_sensitive):
                    matches.append(match)
                    if len(matches) > limit:
                        truncated = True
                        break
                if truncated:
                    break
        return matches[:limit], truncated

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

"""Windows native filesystem backend over the Rust kernel (v0.10 Phase B).

This module is the Windows counterpart of ``linux_backend.py``: it is the
only place in the MCP product path that imports ``serverfs_windows_native``,
and it is imported lazily by ``backends.get_backend`` only on ``win32``.

Lifecycle contract (§10.1, frozen by the Phase A closure review): unlike
the Linux session — which re-opens the root FD per operation exactly as
v0.9 did — the Windows backend acquires the trusted root HANDLE once per
workdir and retains it for the process lifetime. ``open_session`` returns
the cached session; no MCP tool call ever reopens the root.

Channel status (Phase B read kernel, Phase C find/search, Phase D2
mutations): every ``WorkdirSession`` channel is live against the retained
root handle. Mutations map the D1 native kernel (handle-relative atomic
create/replace/delete plus kernel-side read-only enforcement) onto the same
result models and coded errors the Linux backend produces, with the shared
``mutation_text`` semantics so both platforms decide identically.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import mimetypes
import time
from typing import TYPE_CHECKING

import serverfs_windows_native as native

from .backends import BackendError, TextPage
from .binary_payload import BinaryRead, BinaryTransferError
from .concurrency import mutation_lock
from .errors import (
    FileChangedDuringReadError,
    NotAFileError,
    ParentNotFoundError,
    RevisionConflictError,
    WriteTooLargeError,
)
from .models import (
    CreateDirectoryResult,
    CreateTextFileResult,
    DeleteDirectoryResult,
    DeleteFileResult,
    EditTextFileResult,
    EntryInfo,
    StatFileResult,
    TextMatch,
    UploadBinaryFileResult,
)
from .mutation_text import (
    UTF8_BOM,
    apply_edits,
    decode_text,
    encode_new_content,
    validate_edits,
)
from .paths import is_hidden_component
from .search_glob import glob_matches
from .search_scan import ALWAYS_EXCLUDED_DIRS as _RG_ALWAYS_EXCLUDED_DIRS
from .search_scan import rg_binary_truncate as _rg_binary_truncate
from .search_scan import scan_file as _scan_file

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


def _edit_transaction_failure(exc: BackendError, max_write_bytes: int) -> Exception:
    """Map the source transaction's kernel codes onto the edit channel's error vocabulary.

    The kernel answers in the order both backends use — object type, then revision, then the bounded
    read (dev_plan_v0.11 C0.7) — so only those conditions need translating. Everything else already
    carries the agent-visible code and crosses unchanged.
    """
    if exc.code == "NOT_A_FILE":
        return NotAFileError()
    if exc.code == "REVISION_CONFLICT":
        return RevisionConflictError()
    if exc.code == "FILE_TOO_LARGE":
        return WriteTooLargeError(f"file exceeds {max_write_bytes} bytes")
    if exc.code == "FILE_CHANGED_DURING_READ":
        return FileChangedDuringReadError()
    return exc


def _rfc3339_from_100ns(ticks: int) -> str | None:
    if ticks <= 0:
        return None
    seconds = (ticks - _UNIX_EPOCH_100NS) / 10_000_000
    return _dt.datetime.fromtimestamp(seconds, tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# The Linux search pins rg with `--glob !.git/!hg/!svn` regardless of
# allow_hidden (rg never reads VCS internals); the Windows searcher keeps
# that exclusion (ALWAYS_EXCLUDED_DIRS, shared with the Darwin searcher)
# so every backend scans the same set of files.


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
                if glob and not glob_matches(name, rel_from_root, glob):
                    continue
                if size is None or size > max_file_bytes:
                    continue
                if time.monotonic() > deadline:
                    # per-file check: one wide directory must not stretch
                    # the deadline across thousands of reads
                    raise BackendError(
                        "SEARCH_TIMEOUT",
                        f"search exceeded the {timeout_seconds}s deadline",
                    )
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
                data = _rg_binary_truncate(data)
                for match in _scan_file(data, "/".join(child), needle, case_sensitive):
                    matches.append(match)
                    if len(matches) > limit:
                        truncated = True
                        break
                if truncated:
                    break
        return matches[:limit], truncated

    # ---- mutation channels (Phase D2 over the D1 native mutation kernel) ----
    #
    # Every channel maps the plain-data primitives of ``WindowsWorkdirSession``
    # onto the exact v0.9 result models and error codes the Linux backend
    # produces (§11: revision ownership and error normalization are
    # backend-side; text semantics are shared through ``mutation_text`` so
    # both kernels decide identically). The kernel inside ``replace_bytes``
    # / ``delete_file`` holds handles under restrictive sharing, re-checks
    # revision/identity at the final gate and publishes atomically (§19.4);
    # nothing here re-implements that.

    def _ensure_writable(self) -> None:
        # Same precedence as the tool layer: WORKDIR_READ_ONLY is decided
        # before any revision/path condition is even looked at. The native
        # session gate stays as defense in depth behind this.
        if self._workdir.read_only:
            raise BackendError("WORKDIR_READ_ONLY", "workdir is read-only")

    def _publish_create(self, parts: list[str], data: bytes) -> str:
        try:
            with mutation_lock():
                return _call(self._native.create_bytes, parts, data)
        except BackendError as exc:
            if exc.code == "PATH_NOT_FOUND":
                # Linux create channels report a missing parent as
                # PARENT_NOT_FOUND (walk_parent_dirs FileNotFoundError →
                # ParentNotFoundError); the kernel reports PATH_NOT_FOUND
                raise ParentNotFoundError() from None
            raise

    def create_file(
        self, resolved: ResolvedPath, content: str, *, max_write_bytes: int
    ) -> CreateTextFileResult:
        self._ensure_writable()
        data = encode_new_content(content, max_write_bytes)
        revision = self._publish_create(list(resolved.rel_parts), data)
        return CreateTextFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            created=True,
            bytes_written=len(data),
            revision=revision,
        )

    def create_binary_file(
        self, resolved: ResolvedPath, data: bytes, *, max_binary_bytes: int
    ) -> UploadBinaryFileResult:
        self._ensure_writable()
        if len(data) > max_binary_bytes:
            raise WriteTooLargeError(f"binary payload exceeds {max_binary_bytes} bytes")
        revision = self._publish_create(list(resolved.rel_parts), data)
        return UploadBinaryFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            created=True,
            replaced=False,
            bytes_written=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            revision_before=None,
            revision=revision,
        )

    def _replace(self, parts: list[str], data: bytes, expected_revision: str) -> str:
        with mutation_lock():
            return _call(self._native.replace_bytes, parts, data, expected_revision)

    def replace_binary_file(
        self,
        resolved: ResolvedPath,
        data: bytes,
        expected_revision: str,
        *,
        max_binary_bytes: int,
    ) -> UploadBinaryFileResult:
        self._ensure_writable()
        if len(data) > max_binary_bytes:
            raise WriteTooLargeError(f"binary payload exceeds {max_binary_bytes} bytes")
        revision = self._replace(list(resolved.rel_parts), data, expected_revision)
        return UploadBinaryFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            created=False,
            replaced=True,
            bytes_written=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            revision_before=expected_revision,
            revision=revision,
        )

    def replace_file(
        self,
        resolved: ResolvedPath,
        expected_revision: str,
        edits: list[TextEdit],
        *,
        max_write_bytes: int,
        max_edits_per_call: int,
    ) -> EditTextFileResult:
        self._ensure_writable()
        validate_edits(
            edits,
            max_write_bytes=max_write_bytes,
            max_edits_per_call=max_edits_per_call,
        )
        parts = list(resolved.rel_parts)
        # Classification ahead of the transaction, for the *precise* Windows code and the cheap
        # early refusal: `stat` opens the leaf as itself, so a reparse object answers
        # REPARSE_POINT_NOT_ALLOWED rather than the follow-then-type-refusal the mutation open
        # produces. It is not the guard — the transaction below re-answers kind and revision on the
        # handle it holds, which is what Linux also does by opening the target as a regular file
        # (dev_plan_v0.11 C0.7), and v0.10 shipped these codes so they must not drift.
        etype, size, _modified, revision_now = _call(self._native.stat, parts)
        if etype == "directory":
            raise NotAFileError()
        if etype == "reparse_point":
            raise BackendError(
                "REPARSE_POINT_NOT_ALLOWED", "reparse point is not allowed on this channel"
            )
        if revision_now != expected_revision:
            raise RevisionConflictError()
        if size is not None and size > max_write_bytes:
            raise WriteTooLargeError(f"file exceeds {max_write_bytes} bytes")
        measured: dict[str, int | str] = {}

        def build(data: bytes, revision_before: str) -> bytes:
            text, has_bom = decode_text(data)
            body = apply_edits(text, edits).encode("utf-8")
            payload = (UTF8_BOM + body) if has_bom else body
            if len(payload) > max_write_bytes:
                raise WriteTooLargeError(f"result exceeds {max_write_bytes} bytes")
            measured["bytes_before"] = len(data)
            measured["bytes_after"] = len(payload)
            measured["revision_before"] = revision_before
            return payload

        # One transaction: the kernel opens the target with the restricted share, answers object
        # type before the revision guard (C0.7), reads the source from that same held handle and
        # publishes what `build` returns, without releasing the hold in between. That is C0
        # completion contract item 5 in dev_plan_v0.11 §15 — it keeps an *active* external writer
        # out of the read-to-commit window. It does not close the same-tick same-size token alias,
        # which contract decision B accepts and documents.
        with mutation_lock():
            try:
                revision = _call(
                    self._native.replace_bytes_from_source,
                    parts,
                    expected_revision,
                    max_write_bytes,
                    build,
                )
            except BackendError as exc:
                raise _edit_transaction_failure(exc, max_write_bytes) from None
        return EditTextFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            edited=True,
            edits_applied=len(edits),
            bytes_before=int(measured["bytes_before"]),
            bytes_after=int(measured["bytes_after"]),
            revision_before=str(measured["revision_before"]),
            revision=revision,
        )

    def delete_file(self, resolved: ResolvedPath, expected_revision: str) -> DeleteFileResult:
        self._ensure_writable()
        try:
            with mutation_lock():
                bytes_deleted, revision_deleted = _call(
                    self._native.delete_file, list(resolved.rel_parts), expected_revision
                )
        except BackendError as exc:
            if exc.code == "NOT_A_FILE":
                # Linux delete_file goes through the same _open_regular gate
                raise NotAFileError() from None
            raise
        return DeleteFileResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            deleted=True,
            bytes_deleted=bytes_deleted,
            revision_deleted=revision_deleted,
        )

    def create_directory(self, resolved: ResolvedPath) -> CreateDirectoryResult:
        self._ensure_writable()
        try:
            with mutation_lock():
                revision = _call(self._native.create_directory, list(resolved.rel_parts))
        except BackendError as exc:
            if exc.code == "PATH_NOT_FOUND":
                raise ParentNotFoundError() from None
            raise
        return CreateDirectoryResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            created=True,
            revision=revision,
        )

    def delete_directory(
        self, resolved: ResolvedPath, expected_revision: str
    ) -> DeleteDirectoryResult:
        self._ensure_writable()
        with mutation_lock():
            _call(self._native.delete_directory, list(resolved.rel_parts), expected_revision)
        # the kernel verified expected_revision at the final gate before
        # marking the directory for deletion (Linux: same checked value)
        return DeleteDirectoryResult(
            workdir=resolved.workdir.alias,
            path=resolved.rel_path,
            deleted=True,
            revision_deleted=expected_revision,
        )


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

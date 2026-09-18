"""Controlled mutation: revisions, atomic create/edit, delete, mkdir/rmdir.

Every mutation follows the same shape:

    resolve + authorize       (tools layer: policy, workdir read-write)
    → open the workdir root FD
    → walk the PARENT chain by FD (O_DIRECTORY|O_NOFOLLOW)
    → act on the final NAME with dir_fd=parent_fd

so no request-derived path is ever re-assembled into a pathname for the
kernel to re-resolve. Identity checks (fstat on the held FD) and the
mutation (linkat/renameat/unlinkat/mkdirat/rmdirat) therefore refer to the
same objects the policy layer validated.

Publication is always "write a reserved same-directory temp file, fsync,
then publish atomically":

- create: ``linkat`` never overwrites, so an existing target (file, dir,
  symlink, FIFO, socket) makes it fail with PATH_ALREADY_EXISTS — there is
  no check-then-create window
- edit: ``renameat`` replaces the target in one step, so a concurrent
  reader sees either the complete old or the complete new inode

Concurrency model (documented boundary, not a claim):

- every mutation runs under one process-wide lock, so two callers holding
  the same revision cannot both commit
- edit/delete compare the caller's expected_revision against a fresh stat
  taken inside the lock, and re-check it immediately before commit

That is compare-and-swap against *this process* plus a last-moment
re-check. POSIX offers no atomic "compare inode and mutate pathname"
primitive, so a non-cooperating external writer (host user, IDE, another
container) can still rename a pathname between the final check and the
commit. ServerFS does not claim otherwise.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat as stat_module
import threading
from collections.abc import Iterator

from . import fdio
from . import logging as jsonlog
from .config import Settings
from .fdio import stat_at, unlink_at, walk_parent_dirs
from .models import (
    CreateDirectoryResult,
    CreateTextFileResult,
    DeleteDirectoryResult,
    DeleteFileResult,
    EditTextFileResult,
    TextEdit,
)
from .paths import ResolvedPath

REVISION_PREFIX = "v1:"
_REVISION_HEX_CHARS = 16
_UTF8_BOM = b"\xef\xbb\xbf"
_READ_CHUNK = 1 << 20


# ---- coded, agent-safe errors ----


class MutationError(Exception):
    """Base class for anticipated mutation failures (CODE: message)."""

    code = "MUTATION_FAILED"
    message = "mutation failed"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class PathAlreadyExistsError(MutationError):
    code = "PATH_ALREADY_EXISTS"
    message = "target already exists; ServerFS never overwrites"


class ParentNotFoundError(MutationError):
    code = "PARENT_NOT_FOUND"
    message = "parent directory does not exist; create it first"


class NotAFileError(MutationError):
    code = "NOT_A_FILE"
    message = "target is not a regular file"


class RevisionConflictError(MutationError):
    code = "REVISION_CONFLICT"
    message = "content changed since the revision you supplied; re-read and retry"


class FileChangedDuringReadError(MutationError):
    code = "FILE_CHANGED_DURING_READ"
    message = "file changed while it was being read; retry"


class EditConflictError(MutationError):
    code = "EDIT_CONFLICT"
    message = "old_text did not match the expected number of occurrences"


class TooManyEditsError(MutationError):
    code = "TOO_MANY_EDITS"
    message = "too many edits in one call"


class WriteTooLargeError(MutationError):
    code = "WRITE_TOO_LARGE"
    message = "content exceeds the server write size limit"


class BinaryContentError(MutationError):
    code = "BINARY_CONTENT_NOT_ALLOWED"
    message = "content must be UTF-8 text without NUL bytes"


class BinaryFileError(MutationError):
    code = "BINARY_FILE"
    message = "file appears to be binary"


class UnsupportedTextEncodingError(MutationError):
    code = "UNSUPPORTED_TEXT_ENCODING"
    message = "file is not valid UTF-8"


class MultipleHardlinksError(MutationError):
    code = "MULTIPLE_HARDLINKS_NOT_SUPPORTED"
    message = "file has multiple hard links; editing would break them"


class MetadataPreservationError(MutationError):
    code = "METADATA_PRESERVATION_FAILED"
    message = "file metadata could not be preserved; nothing was changed"


class DirectoryNotEmptyError(MutationError):
    code = "DIRECTORY_NOT_EMPTY"
    message = "directory is not empty; ServerFS never deletes recursively"


class MutationIOError(MutationError):
    code = "MUTATION_IO_ERROR"
    message = "the filesystem refused the operation"


# ---- revision ----


def compute_revision(st: os.stat_result) -> str:
    """Opaque revision token for one filesystem object.

    Derived from the full stat tuple (identity, mode, ownership, size,
    timestamps, link count) so any content or metadata change yields a new
    token. The digest is what makes it safe to hand to an agent: inode
    numbers, UIDs and GIDs never leave the process in cleartext.
    """
    material = ":".join(
        str(value)
        for value in (
            st.st_dev,
            st.st_ino,
            st.st_mode,
            st.st_uid,
            st.st_gid,
            st.st_size,
            st.st_mtime_ns,
            st.st_ctime_ns,
            st.st_nlink,
        )
    )
    digest = hashlib.sha256(material.encode("ascii")).hexdigest()
    return REVISION_PREFIX + digest[:_REVISION_HEX_CHARS]


# ---- process-local serialization ----


_MUTATION_LOCK = threading.Lock()


@contextlib.contextmanager
def mutation_lock() -> Iterator[None]:
    """Serialize every mutation in this process.

    Reads deliberately do not take this lock. One lock, not a per-path
    table: mutation traffic is low, and correctness here beats throughput.
    """
    with _MUTATION_LOCK:
        yield


# ---- shared plumbing ----


def _root_fd(resolved: ResolvedPath):
    """Root-FD context manager for a resolved workdir path (fdio.root_fd)."""
    return fdio.root_fd(str(resolved.workdir.container_path))


@contextlib.contextmanager
def _parent_of(root_fd: int, rel_parts: tuple[str, ...]) -> Iterator[tuple[int, str]]:
    """Yield (parent directory FD, final name) for a validated rel path."""
    *parents, name = rel_parts
    with walk_parent_dirs(root_fd, tuple(parents)) as parent_fd:
        yield parent_fd, name


@contextlib.contextmanager
def _open_regular(parent_fd: int, name: str) -> Iterator[int]:
    """open_regular_at, reported with file-operation error codes."""
    try:
        with fdio.open_regular_at(parent_fd, name) as fd:
            yield fd
    except (IsADirectoryError, NotADirectoryError):
        raise NotAFileError() from None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - the kernel never short-writes a file
            raise MutationIOError("short write")
        view = view[written:]


def _read_all(fd: int, limit: int) -> bytes:
    """Read a whole file, refusing to buffer more than limit bytes."""
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, min(remaining, _READ_CHUNK))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise WriteTooLargeError(f"file exceeds {limit} bytes")
    return data


def _utf8_size(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise BinaryContentError("text is not valid UTF-8") from None


def _persist_directory(parent_fd: int) -> None:
    """Persist a committed directory entry (create/link/replace/unlink).

    Every caller reaches this AFTER the mutation is visible to readers, so a
    failed fsync means "not durable across a crash", not "nothing happened".
    Failing the tool call here would contradict what the filesystem already
    shows and invite a pointless retry, so the operator gets a warning and
    the agent gets the truth about the mutation.
    """
    try:
        fdio.fsync_directory(parent_fd)
    except OSError as exc:
        jsonlog.warning("directory_fsync_failed", errno=exc.errno)


def _decode_text(data: bytes) -> tuple[str, bool]:
    """Decode a text file, hiding the physical BOM from the caller."""
    has_bom = data.startswith(_UTF8_BOM)
    body = data[3:] if has_bom else data
    if b"\x00" in body:
        raise BinaryFileError()
    try:
        return body.decode("utf-8"), has_bom
    except UnicodeDecodeError:
        raise UnsupportedTextEncodingError() from None


def _preserve_metadata(src_fd: int, dst_fd: int, st: os.stat_result) -> None:
    """Copy mode, ownership and xattrs onto the replacement inode.

    A rename publishes a different inode, so anything not copied here is
    silently lost. When a piece cannot be preserved the mutation fails
    before the rename: losing a mode bit, an ACL or a SELinux label is
    worse than a failed edit.

    The order — ownership, then mode, then xattrs — is load-bearing rather
    than cosmetic; see the comments at each step.
    """
    dst_st = os.fstat(dst_fd)
    if (dst_st.st_uid, dst_st.st_gid) != (st.st_uid, st.st_gid):
        try:
            os.fchown(dst_fd, st.st_uid, st.st_gid)
        except OSError as exc:
            raise MetadataPreservationError("ownership could not be preserved") from exc
    # ownership first: chown(2) clears S_ISUID/S_ISGID, so the mode has to
    # be applied after it — in the other order the replacement silently
    # loses those bits
    try:
        os.fchmod(dst_fd, stat_module.S_IMODE(st.st_mode))
    except OSError as exc:
        raise MetadataPreservationError("file mode could not be preserved") from exc
    # xattrs last, from the original: the ownership change above can
    # disturb security.* metadata, and the replacement must end up
    # holding what the original held, not what a chown left behind
    _copy_xattrs(src_fd, dst_fd)


def _copy_xattrs(src_fd: int, dst_fd: int) -> None:
    try:
        names = os.listxattr(src_fd)
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            # the filesystem stores no xattrs at all, so none can be lost
            return
        raise MetadataPreservationError("extended attributes could not be read") from exc
    for attr in names:
        try:
            value = os.getxattr(src_fd, attr)
        except OSError as exc:
            if exc.errno == errno.ENODATA:
                continue  # removed concurrently: nothing left to preserve
            raise MetadataPreservationError("extended attributes could not be read") from exc
        try:
            os.setxattr(dst_fd, attr, value)
        except OSError as exc:
            raise MetadataPreservationError("extended attributes could not be preserved") from exc


# ---- create ----


def _encode_new_content(content: str, settings: Settings) -> bytes:
    if "\x00" in content:
        raise BinaryContentError()
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise BinaryContentError("text is not valid UTF-8") from None
    if len(data) > settings.max_write_bytes:
        raise WriteTooLargeError(f"content exceeds {settings.max_write_bytes} bytes")
    return data


def _publish_new_file(parent_fd: int, name: str, data: bytes) -> str:
    """Create ``name`` atomically; never overwrites. Returns the revision.

    linkat(2) fails with EEXIST when the target exists — including when it
    is a symlink, FIFO or directory — so "does not exist" and "is now this
    content" are one atomic step, not a checked precondition.
    """
    temp_name, temp_fd = fdio.create_temp_at(parent_fd)
    try:
        _write_all(temp_fd, data)
        os.fsync(temp_fd)
        try:
            os.link(
                temp_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise PathAlreadyExistsError() from None
        # Dropping the temp name changes ctime and nlink, so the revision is
        # read AFTER publication — otherwise the token handed back here would
        # not match the next stat of the file being created.
        unlink_at(parent_fd, temp_name)
        revision = compute_revision(os.fstat(temp_fd))
    finally:
        # after a successful link this drops the temp name; on any failure
        # it removes the debris. Best-effort by design.
        unlink_at(parent_fd, temp_name)
        os.close(temp_fd)
    _persist_directory(parent_fd)
    return revision


def create_text_file(
    resolved: ResolvedPath, content: str, settings: Settings
) -> CreateTextFileResult:
    """Create a new UTF-8 text file; the target must not exist."""
    data = _encode_new_content(content, settings)
    with mutation_lock(), _root_fd(resolved) as root_fd:
        with contextlib.ExitStack() as stack:
            try:
                parent_fd, name = stack.enter_context(_parent_of(root_fd, resolved.rel_parts))
            except FileNotFoundError:
                raise ParentNotFoundError() from None
            revision = _publish_new_file(parent_fd, name, data)
    return CreateTextFileResult(
        workdir=resolved.workdir.alias,
        path=resolved.rel_path,
        created=True,
        bytes_written=len(data),
        revision=revision,
    )


def create_directory(resolved: ResolvedPath) -> CreateDirectoryResult:
    """Create one directory; the parent must already exist (no mkdir -p)."""
    with mutation_lock(), _root_fd(resolved) as root_fd:
        with contextlib.ExitStack() as stack:
            try:
                parent_fd, name = stack.enter_context(_parent_of(root_fd, resolved.rel_parts))
            except FileNotFoundError:
                raise ParentNotFoundError() from None
            try:
                os.mkdir(name, 0o777, dir_fd=parent_fd)
            except FileExistsError:
                raise PathAlreadyExistsError() from None
            _persist_directory(parent_fd)
            revision = compute_revision(stat_at(parent_fd, name))
    return CreateDirectoryResult(
        workdir=resolved.workdir.alias,
        path=resolved.rel_path,
        created=True,
        revision=revision,
    )


# ---- edit ----


def _apply_edits(text: str, edits: list[TextEdit]) -> str:
    """Apply exact-match edits in order to the in-memory text.

    Nothing is written until every edit has been validated and applied, so
    one failing edit leaves the file untouched.
    """
    for edit in edits:
        old_text = edit.old_text
        if old_text == "" and (text != "" or edit.expected_count != 1):
            # the only legal empty match is filling a completely empty file
            raise EditConflictError("empty old_text is only allowed when the file is empty")
        found = text.count(old_text)
        if found != edit.expected_count:
            raise EditConflictError(f"expected {edit.expected_count} occurrence(s), found {found}")
        text = text.replace(old_text, edit.new_text)
    return text


def _validate_edits(edits: list[TextEdit], settings: Settings) -> None:
    if not edits:
        raise EditConflictError("no edits supplied")
    if len(edits) > settings.max_edits_per_call:
        raise TooManyEditsError(f"at most {settings.max_edits_per_call} edits per call")
    for edit in edits:
        # The source file is verified NUL-free, so a request that carries no
        # NUL cannot produce a binary result — and edit must not become the
        # one way to create a file no text channel can read again.
        if "\x00" in edit.old_text or "\x00" in edit.new_text:
            raise BinaryContentError()
    total = sum(_utf8_size(e.old_text) + _utf8_size(e.new_text) for e in edits)
    if total > settings.max_write_bytes:
        raise WriteTooLargeError(f"edits exceed {settings.max_write_bytes} bytes")


def _replace_at(
    parent_fd: int,
    name: str,
    payload: bytes,
    src_fd: int,
    original: os.stat_result,
    expected_revision: str,
) -> str:
    """Atomically replace ``name`` with ``payload``, preserving metadata."""
    temp_name, temp_fd = fdio.create_temp_at(parent_fd)
    try:
        _write_all(temp_fd, payload)
        _preserve_metadata(src_fd, temp_fd, original)
        os.fsync(temp_fd)
        # last-moment re-check, inside the lock: the target must still be
        # the exact object the caller read
        if compute_revision(stat_at(parent_fd, name)) != expected_revision:
            raise RevisionConflictError()
        os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        # rename(2) updates the moved inode's ctime: read the revision after
        # the commit so it matches the next stat of the edited file
        revision = compute_revision(os.fstat(temp_fd))
    finally:
        unlink_at(parent_fd, temp_name)
        os.close(temp_fd)
    _persist_directory(parent_fd)
    return revision


def edit_text_file(
    resolved: ResolvedPath,
    expected_revision: str,
    edits: list[TextEdit],
    settings: Settings,
) -> EditTextFileResult:
    """Replace exact text in an existing UTF-8 file; never creates one."""
    _validate_edits(edits, settings)
    with mutation_lock(), _root_fd(resolved) as root_fd:
        with _parent_of(root_fd, resolved.rel_parts) as (parent_fd, name):
            with _open_regular(parent_fd, name) as fd:
                before = os.fstat(fd)
                revision_before = compute_revision(before)
                if revision_before != expected_revision:
                    raise RevisionConflictError()
                if before.st_nlink > 1:
                    raise MultipleHardlinksError()
                if before.st_size > settings.max_write_bytes:
                    raise WriteTooLargeError(f"file exceeds {settings.max_write_bytes} bytes")
                data = _read_all(fd, settings.max_write_bytes)
                if compute_revision(os.fstat(fd)) != expected_revision:
                    raise FileChangedDuringReadError()
                text, has_bom = _decode_text(data)
                edited_text = _apply_edits(text, edits)
                body = edited_text.encode("utf-8")
                payload = (_UTF8_BOM + body) if has_bom else body
                if len(payload) > settings.max_write_bytes:
                    raise WriteTooLargeError(f"result exceeds {settings.max_write_bytes} bytes")
                revision = _replace_at(parent_fd, name, payload, fd, before, expected_revision)
    return EditTextFileResult(
        workdir=resolved.workdir.alias,
        path=resolved.rel_path,
        edited=True,
        edits_applied=len(edits),
        bytes_before=before.st_size,
        bytes_after=len(payload),
        revision_before=revision_before,
        revision=revision,
    )


# ---- delete ----


def delete_file(resolved: ResolvedPath, expected_revision: str) -> DeleteFileResult:
    """Delete one regular file (any content type; text is not required)."""
    with mutation_lock(), _root_fd(resolved) as root_fd:
        with _parent_of(root_fd, resolved.rel_parts) as (parent_fd, name):
            with _open_regular(parent_fd, name) as fd:
                st = os.fstat(fd)
                revision = compute_revision(st)
                if revision != expected_revision:
                    raise RevisionConflictError()
                if compute_revision(stat_at(parent_fd, name)) != expected_revision:
                    raise RevisionConflictError()
                # unlink by NAME while still holding the verified FD: the
                # object we checked is the object whose directory entry goes
                os.unlink(name, dir_fd=parent_fd)
            _persist_directory(parent_fd)
    return DeleteFileResult(
        workdir=resolved.workdir.alias,
        path=resolved.rel_path,
        deleted=True,
        bytes_deleted=st.st_size,
        revision_deleted=revision,
    )


def delete_directory(resolved: ResolvedPath, expected_revision: str) -> DeleteDirectoryResult:
    """Delete one EMPTY directory; never recursive."""
    with mutation_lock(), _root_fd(resolved) as root_fd:
        with _parent_of(root_fd, resolved.rel_parts) as (parent_fd, name):
            with fdio.open_dir_at(parent_fd, name) as dir_fd:
                st = os.fstat(dir_fd)
                revision = compute_revision(st)
                if revision != expected_revision:
                    raise RevisionConflictError()
                # physical emptiness: hidden, denied and reserved entries
                # count too — never a filtered view of the directory
                with os.scandir(dir_fd) as entries:
                    for _ in entries:
                        raise DirectoryNotEmptyError()
                if compute_revision(stat_at(parent_fd, name)) != expected_revision:
                    raise RevisionConflictError()
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except OSError as exc:
                    if exc.errno == errno.ENOTEMPTY:
                        # something appeared after the emptiness check
                        raise DirectoryNotEmptyError() from None
                    raise
            _persist_directory(parent_fd)
    return DeleteDirectoryResult(
        workdir=resolved.workdir.alias,
        path=resolved.rel_path,
        deleted=True,
        revision_deleted=revision,
    )

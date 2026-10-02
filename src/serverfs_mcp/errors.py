"""Platform-neutral coded error contract used by the product layer.

These exception classes carry the agent-facing ``code``/``message`` pair and
nothing else: no file descriptors, no errno, no platform objects. The tool
layer maps them to ``CODE: message`` ToolErrors, so it must be able to
recognize them without importing any Linux-only kernel module (§11 import
safety). Platform kernels raise them; the mutation kernel
(``mutations.py``) defines its subclasses on top of ``MutationError``.
"""

from __future__ import annotations


class MutationError(Exception):
    """Base class for anticipated mutation failures (CODE: message)."""

    code = "MUTATION_FAILED"
    message = "mutation failed"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class AgentLeaseError(Exception):
    code = "AGENT_LOCK_UNAVAILABLE"
    message = "shared Agent workdir lease is unavailable"


class WorkdirBusyError(AgentLeaseError):
    code = "WORKDIR_BUSY"
    message = "workdir is busy with an active Agent task"


class WorkdirRecoveryRequiredError(AgentLeaseError):
    code = "WORKDIR_RECOVERY_REQUIRED"
    message = "workdir has unresolved Agent recovery state"


# ---- mutation failure codes ----
#
# Defined here (kernel-free) rather than in mutations.py so every platform
# backend raises the identical code/message pair through one set of classes
# without importing the Linux fdio kernel (§11 import safety, channels
# filter/report identically).


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


__all__ = [
    "AgentLeaseError",
    "BinaryContentError",
    "BinaryFileError",
    "DirectoryNotEmptyError",
    "EditConflictError",
    "FileChangedDuringReadError",
    "MetadataPreservationError",
    "MutationError",
    "MutationIOError",
    "MultipleHardlinksError",
    "NotAFileError",
    "ParentNotFoundError",
    "PathAlreadyExistsError",
    "RevisionConflictError",
    "TooManyEditsError",
    "UnsupportedTextEncodingError",
    "WorkdirBusyError",
    "WorkdirRecoveryRequiredError",
    "WriteTooLargeError",
]

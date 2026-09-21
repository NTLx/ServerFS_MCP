"""Bounded raw-byte reads for the optional binary transfer channel.

This module deliberately does not resolve user paths or make authorization
decisions. Callers pass a policy-validated ResolvedPath; filesystem identity is
then anchored by the same fdio root/file primitives used by the text channel.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import os
from dataclasses import dataclass

from .fdio import open_file_fd, root_fd
from .mutations import compute_revision
from .paths import ResolvedPath

_READ_CHUNK = 64 * 1024


class BinaryTransferError(Exception):
    """Expected binary-transfer failure with an agent-safe error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BinaryRead:
    """Exact bytes plus integrity metadata from one unchanged regular file."""

    data: bytes
    size: int
    mime_type: str
    sha256: str
    revision: str


def decode_base64_payload(payload: str, *, max_bytes: int) -> bytes:
    """Strictly decode one whole-file payload under the effective byte limit.

    The encoded-length check happens before allocating decoded bytes. Strict
    validation rejects whitespace, non-alphabet characters and malformed
    padding instead of silently normalizing caller input.
    """
    max_encoded = 4 * ((max_bytes + 2) // 3)
    if len(payload) > max_encoded:
        raise BinaryTransferError(
            "BINARY_PAYLOAD_TOO_LARGE",
            f"decoded payload would exceed the {max_bytes}-byte binary transfer limit",
        )
    try:
        encoded = payload.encode("ascii")
    except UnicodeEncodeError:
        raise BinaryTransferError("INVALID_BASE64", "payload is not ASCII base64") from None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise BinaryTransferError("INVALID_BASE64", "payload is not valid base64") from None
    if len(data) > max_bytes:
        raise BinaryTransferError(
            "BINARY_PAYLOAD_TOO_LARGE",
            f"payload exceeds the {max_bytes}-byte binary transfer limit",
        )
    return data


def read_binary_file(resolved: ResolvedPath, *, max_bytes: int) -> BinaryRead:
    """Read one regular file exactly, bounded by max_bytes.

    The same open file descriptor is fstat'ed before and after reading. If its
    revision changes, no bytes are returned to the caller.
    """
    with root_fd(str(resolved.workdir.container_path)) as root:
        with open_file_fd(root, resolved.rel_parts) as fd:
            before = os.fstat(fd)
            revision = compute_revision(before)
            if before.st_size > max_bytes:
                raise BinaryTransferError(
                    "BINARY_FILE_TOO_LARGE",
                    f"file exceeds the {max_bytes}-byte binary transfer limit",
                )

            chunks: list[bytes] = []
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(fd, _READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise BinaryTransferError(
                        "BINARY_FILE_TOO_LARGE",
                        f"file exceeds the {max_bytes}-byte binary transfer limit",
                    )
                chunks.append(chunk)
                digest.update(chunk)

            if compute_revision(os.fstat(fd)) != revision:
                raise BinaryTransferError(
                    "FILE_CHANGED_DURING_READ",
                    "file changed while being read; retry",
                )

    mime_type = mimetypes.guess_type(resolved.rel_path, strict=False)[0]
    return BinaryRead(
        data=b"".join(chunks),
        size=total,
        mime_type=mime_type or "application/octet-stream",
        sha256=digest.hexdigest(),
        revision=revision,
    )

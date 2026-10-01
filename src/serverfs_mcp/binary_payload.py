"""Platform-neutral binary-transfer payload layer.

Base64 decoding, the transfer error code and the plain-data read result are
pure byte/string handling with no filesystem channel: the MCP tool layer and
the file-ingress client import them from here so neither drags the Linux
fdio kernel into its import graph (§11 import safety). The actual bounded
FD-based byte *read* stays in ``binary.py`` behind the Linux session.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass


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


__all__ = ["BinaryRead", "BinaryTransferError", "decode_base64_payload"]

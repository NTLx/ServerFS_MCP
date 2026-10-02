"""Platform-neutral text-mutation semantics shared by every backend kernel.

These are the pure decisions the mutation contract makes before any
platform object is touched: content encoding, edit validation/application,
and UTF-8/BOM/NUL decoding. They lived beside the Linux fdio walk in
mutations.py; extracting them keeps the Windows backend from re-implementing
them (a second text-semantics implementation is a defect, not a shortcut),
and this module must never import fdio, fcntl or the native extension.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import (
    BinaryContentError,
    BinaryFileError,
    EditConflictError,
    TooManyEditsError,
    UnsupportedTextEncodingError,
    WriteTooLargeError,
)

if TYPE_CHECKING:
    from .models import TextEdit

UTF8_BOM = b"\xef\xbb\xbf"


def utf8_size(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise BinaryContentError("text is not valid UTF-8") from None


def encode_new_content(content: str, max_write_bytes: int) -> bytes:
    if "\x00" in content:
        raise BinaryContentError()
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise BinaryContentError("text is not valid UTF-8") from None
    if len(data) > max_write_bytes:
        raise WriteTooLargeError(f"content exceeds {max_write_bytes} bytes")
    return data


def validate_edits(edits: list[TextEdit], *, max_write_bytes: int, max_edits_per_call: int) -> None:
    if not edits:
        raise EditConflictError("no edits supplied")
    if len(edits) > max_edits_per_call:
        raise TooManyEditsError(f"at most {max_edits_per_call} edits per call")
    for edit in edits:
        # The source file is verified NUL-free, so a request that carries no
        # NUL cannot produce a binary result — and edit must not become the
        # one way to create a file no text channel can read again.
        if "\x00" in edit.old_text or "\x00" in edit.new_text:
            raise BinaryContentError()
    total = sum(utf8_size(e.old_text) + utf8_size(e.new_text) for e in edits)
    if total > max_write_bytes:
        raise WriteTooLargeError(f"edits exceed {max_write_bytes} bytes")


def apply_edits(text: str, edits: list[TextEdit]) -> str:
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


def decode_text(data: bytes) -> tuple[str, bool]:
    """Decode a text file, hiding the physical BOM from the caller."""
    has_bom = data.startswith(UTF8_BOM)
    body = data[3:] if has_bom else data
    if b"\x00" in body:
        raise BinaryFileError()
    try:
        return body.decode("utf-8"), has_bom
    except UnicodeDecodeError:
        raise UnsupportedTextEncodingError() from None


__all__ = [
    "UTF8_BOM",
    "apply_edits",
    "decode_text",
    "encode_new_content",
    "utf8_size",
    "validate_edits",
]

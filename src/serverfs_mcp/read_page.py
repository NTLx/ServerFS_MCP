"""Paginated UTF-8 text read over an already-anchored regular-file FD.

Shared by the Linux and Darwin sessions: both platforms open the workdir
root and the file by FD (``posix_fdio``), then page the bytes with this one
implementation. The revision is computed before and after the read so a
concurrent mutation aborts the page instead of returning torn content.
"""

from __future__ import annotations

import os

from .backends import BackendError, TextPage


def read_page_from_fd(
    fd: int,
    *,
    start_line: int,
    max_lines: int,
    max_read_bytes: int,
    binary_sample: int,
) -> TextPage:
    """Paginated UTF-8 text read with before/after revision stability.

    compute_revision is resolved at CALL time (attribute lookup on the
    mutations module) so tests can monkeypatch it as the revision
    authority of this backend.
    """
    from . import mutations

    revision_of = mutations.compute_revision
    revision = revision_of(os.fstat(fd))
    with open(fd, "rb", closefd=False) as fh:
        sample = fh.read(binary_sample)
        has_nul = b"\x00" in sample
        bom = sample.startswith(b"\xef\xbb\xbf")
        fh.seek(0)
        lines: list[bytes] = []
        line_no = 0
        bytes_returned = 0
        end_line = start_line - 1
        has_more = False
        for raw in fh:
            line_no += 1
            if line_no == 1 and bom:
                raw = raw[3:]
            if line_no < start_line:
                continue
            if len(raw) > max_read_bytes:
                raise BackendError(
                    "LINE_TOO_LARGE", f"line {line_no} exceeds {max_read_bytes} bytes"
                )
            if len(lines) >= max_lines:
                has_more = True
                break
            if bytes_returned + len(raw) > max_read_bytes:
                has_more = True
                break
            lines.append(raw)
            bytes_returned += len(raw)
            end_line = line_no
    if revision_of(os.fstat(fd)) != revision:
        raise BackendError("FILE_CHANGED_DURING_READ", "file changed while being read")

    return TextPage(
        revision=revision,
        lines=lines,
        bytes_returned=bytes_returned,
        end_line=end_line,
        has_more=has_more,
        has_nul=has_nul,
        has_bom=bom,
    )

"""Windows Named-Pipe client ABI regression tests."""

from __future__ import annotations

import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Named Pipe Agent endpoint")

if sys.platform == "win32":
    from serverfs_mcp.windows_agent_pipe import (
        ERROR_BROKEN_PIPE,
        ERROR_FILE_NOT_FOUND,
        ERROR_MORE_DATA,
        ERROR_NO_DATA,
        ERROR_PIPE_BUSY,
        ERROR_PIPE_NOT_CONNECTED,
        TRANSIENT_OPEN_ERRORS,
        BridgePipe,
    )


def test_named_pipe_error_constants_match_winerror_h() -> None:
    """Retry/EOF classification must use the Win32 ABI values, not copied aliases."""
    assert ERROR_BROKEN_PIPE == 109
    assert ERROR_PIPE_BUSY == 231
    assert ERROR_NO_DATA == 232
    assert ERROR_PIPE_NOT_CONNECTED == 233
    assert ERROR_MORE_DATA == 234


def test_transient_open_errors_are_only_connect_retry_conditions() -> None:
    assert TRANSIENT_OPEN_ERRORS == {
        ERROR_FILE_NOT_FOUND,
        ERROR_PIPE_BUSY,
        ERROR_PIPE_NOT_CONNECTED,
    }
    assert ERROR_BROKEN_PIPE not in TRANSIENT_OPEN_ERRORS


def test_buffered_response_line_cannot_bypass_the_size_limit() -> None:
    pipe = BridgePipe(0, "test")
    pipe._buffer.extend(b"12345678\n")
    with pytest.raises(ValueError, match="response exceeds configured limit"):
        pipe.read_line(limit=8, deadline=time.monotonic())
    assert pipe._buffer == bytearray()

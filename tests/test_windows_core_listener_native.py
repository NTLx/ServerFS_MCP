"""Native Windows-only kernel socket owner acceptance, no ServerFS services.

Creates one ephemeral 127.0.0.1 listening socket, reads Microsoft's
GetExtendedTcpTable and closes the socket. Does not bind configured Core ports.
"""

from __future__ import annotations

import os
import socket
import sys

import pytest

from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows kernel only")


def test_ephemeral_loopback_socket_has_this_process_pid() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert port != 0
        assert any(
            row.address == "127.0.0.1" and row.port == port and row.pid == os.getpid()
            for row in _windows_ipv4_tcp_listeners()
        )


def test_closed_ephemeral_listener_no_longer_in_kernel_table() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)
        assert any(
            row.port == port and row.pid == os.getpid() for row in _windows_ipv4_tcp_listeners()
        )
    finally:
        server.close()
    # An exact ephemeral LISTEN row must be absent after the owning socket
    # was closed. Other processes may later reuse the port, so check PID too.
    assert not any(
        row.port == port and row.pid == os.getpid() for row in _windows_ipv4_tcp_listeners()
    )

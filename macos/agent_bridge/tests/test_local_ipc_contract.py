"""Darwin local IPC identity and bounds contract; Windows SID tests live in windows/."""

from __future__ import annotations

import os
import socket
import sys

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.local_ipc import (
    authorize_darwin_peer,
    max_socket_path_bytes,
    measure_darwin_peer,
)
from serverfs_agent_bridge.protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin IPC")


def test_measured_peer_and_authorization() -> None:
    left, right = socket.socketpair()
    try:
        peer = measure_darwin_peer(left)
        assert peer.uid == os.getuid()
        assert peer.gid == os.getgid()
        assert not hasattr(peer, "pid")
        authorize_darwin_peer(peer, allowed_uid=peer.uid, allowed_gid=peer.gid)
        for uid, gid in ((peer.uid + 1, peer.gid), (peer.uid, peer.gid + 1)):
            with pytest.raises(BridgeError) as denied:
                authorize_darwin_peer(peer, allowed_uid=uid, allowed_gid=gid)
            assert denied.value.code == "PEER_NOT_AUTHORIZED"
    finally:
        left.close()
        right.close()


def test_unmeasured_peer_fails_closed() -> None:
    with pytest.raises(BridgeError) as denied:
        measure_darwin_peer(None)
    assert denied.value.code == "PEER_NOT_AUTHORIZED"


def test_frozen_ipc_bounds() -> None:
    assert PROTOCOL_VERSION == 1
    assert MAX_REQUEST_BYTES == MAX_RESPONSE_BYTES == 1_048_576
    assert max_socket_path_bytes() == 103

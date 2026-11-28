"""Linux local IPC identity and endpoint helpers."""

from __future__ import annotations

import socket
import struct

from .errors import BridgeError

SOCKET_PATH_LIMIT_BYTES = 107


def max_socket_path_bytes() -> int:
    return SOCKET_PATH_LIMIT_BYTES


class PosixPeer:
    """``SO_PEERCRED`` of the connecting process: uid/gid are authoritative."""

    __slots__ = ("uid", "gid", "pid")

    def __init__(self, uid: int, gid: int, pid: int):
        self.uid = uid
        self.gid = gid
        self.pid = pid

    def __repr__(self) -> str:
        return f"PosixPeer(uid={self.uid}, gid={self.gid}, pid={self.pid})"


def measure_posix_peer(sock: socket.socket | None) -> PosixPeer:
    if sock is None or not hasattr(socket, "SO_PEERCRED"):
        raise BridgeError("PEER_NOT_AUTHORIZED", "peer credentials are unavailable")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError as exc:
        raise BridgeError("PEER_NOT_AUTHORIZED", "peer credentials are unavailable") from exc
    pid, uid, gid = struct.unpack("3i", raw)
    return PosixPeer(uid=uid, gid=gid, pid=pid)


def authorize_posix_peer(
    peer: PosixPeer,
    *,
    allowed_uid: int | None,
    allowed_gid: int | None,
) -> None:
    if allowed_uid is not None and peer.uid != allowed_uid:
        raise BridgeError("PEER_NOT_AUTHORIZED", "peer uid is not authorized")
    if allowed_gid is not None and peer.gid != allowed_gid:
        raise BridgeError("PEER_NOT_AUTHORIZED", "peer gid is not authorized")

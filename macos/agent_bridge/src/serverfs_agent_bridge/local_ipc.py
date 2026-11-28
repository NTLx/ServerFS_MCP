"""macOS local IPC identity and endpoint helpers."""

from __future__ import annotations

import ctypes
import ctypes.util
import socket

from .errors import BridgeError

SOCKET_PATH_LIMIT_BYTES = 103


def max_socket_path_bytes() -> int:
    return SOCKET_PATH_LIMIT_BYTES


class DarwinPeer:
    """``getpeereid`` of the connecting process: euid/egid are authoritative.

    Darwin exposes no ``SO_PEERCRED`` and ServerFS fabricates no peer PID
    (dev_plan_v0.13.md §11 D3): identity is exactly the measured euid/egid.
    """

    __slots__ = ("uid", "gid")

    def __init__(self, uid: int, gid: int):
        self.uid = uid
        self.gid = gid

    def __repr__(self) -> str:
        return f"DarwinPeer(uid={self.uid}, gid={self.gid})"


def measure_darwin_peer(sock: socket.socket | None) -> DarwinPeer:
    """The authoritative peer euid/egid of an AF_UNIX connection, via getpeereid."""
    if sock is None:
        raise BridgeError("PEER_NOT_AUTHORIZED", "peer credentials are unavailable")
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.getpeereid.restype = ctypes.c_int
    libc.getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    uid = ctypes.c_uint32(0)
    gid = ctypes.c_uint32(0)
    rc = libc.getpeereid(ctypes.c_int(sock.fileno()), ctypes.byref(uid), ctypes.byref(gid))
    if rc != 0:
        raise BridgeError("PEER_NOT_AUTHORIZED", "peer credentials are unavailable")
    return DarwinPeer(uid=uid.value, gid=gid.value)


def authorize_darwin_peer(
    peer: DarwinPeer,
    *,
    allowed_uid: int | None,
    allowed_gid: int | None,
) -> None:
    if allowed_uid is not None and peer.uid != allowed_uid:
        raise BridgeError("PEER_NOT_AUTHORIZED", "peer uid is not authorized")
    if allowed_gid is not None and peer.gid != allowed_gid:
        raise BridgeError("PEER_NOT_AUTHORIZED", "peer gid is not authorized")

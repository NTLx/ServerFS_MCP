"""The §3 local-IPC and peer-identity seams: portable facade.

The Windows endpoint implementation lives in ``windows_ipc`` and is imported lazily, so a
Linux Bridge process never loads a Win32 module and the frozen Linux path is unchanged.

The pipe name contract (§6) lives here because both platforms' callers must agree on it: it
is a deterministic function of the current user's SID, and it is disambiguation only —
authorization comes from the pipe DACL plus the measured client SID.
"""

from __future__ import annotations

import hashlib
import socket
import struct

from .errors import BridgeError

#: ``get_extra_info`` key under which a Windows connection exposes its measured peer.
PEER_INFO_KEY = "serverfs_bridge_peer"

#: §6: user-scoped deterministic pipe name.
PIPE_NAMESPACE = "\\\\.\\pipe\\"
PIPE_NAME_PREFIX = PIPE_NAMESPACE + "serverfs-agent-bridge-v1-"
PIPE_NAME_HASH_LENGTH = 16


def derive_pipe_name(user_sid: str) -> str:
    r"""The deterministic pipe name for one Windows user (§6).

    The suffix is a hash of the user's SID string — never the username, host name, PID or a
    random value — so ServerFS and the Bridge derive the same name independently and two users'
    Bridges cannot collide. The name is disambiguation only; it is not an authentication factor.
    """
    if not user_sid.startswith("S-"):
        raise BridgeError("BRIDGE_IDENTITY_UNAVAILABLE", "the pipe name needs a canonical SID")
    digest = hashlib.sha256(user_sid.upper().encode("ascii")).hexdigest()
    return PIPE_NAME_PREFIX + digest[:PIPE_NAME_HASH_LENGTH]


class PosixPeer:
    """``SO_PEERCRED`` of the connecting process: uid/gid are authoritative."""

    __slots__ = ("uid", "gid", "pid")

    def __init__(self, uid: int, gid: int, pid: int):
        self.uid = uid
        self.gid = gid
        self.pid = pid

    def __repr__(self) -> str:
        return f"PosixPeer(uid={self.uid}, gid={self.gid}, pid={self.pid})"


class WindowsPeer:
    """The impersonated client of one pipe instance: the SID is authoritative (§11)."""

    __slots__ = ("sid", "pid", "session_id", "impersonation_level", "token_type")

    def __init__(
        self,
        sid: str,
        pid: int,
        session_id: int,
        impersonation_level: int,
        token_type: int,
    ):
        self.sid = sid
        self.pid = pid
        self.session_id = session_id
        self.impersonation_level = impersonation_level
        self.token_type = token_type

    def __repr__(self) -> str:
        # The SID is deliberately absent so identity material cannot reach an ordinary log.
        return (
            f"WindowsPeer(pid={self.pid}, session_id={self.session_id}, "
            f"impersonation_level={self.impersonation_level}, token_type={self.token_type})"
        )


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


def authorize_windows_peer(peer: WindowsPeer | None, *, allowed_sid: str) -> None:
    """Assert the identity the transport measured. The pipe name is never trusted (§9)."""
    if peer is None:
        raise BridgeError("PEER_NOT_AUTHORIZED", "peer identity was never measured")
    if peer.sid != allowed_sid:
        raise BridgeError("PEER_NOT_AUTHORIZED", "peer sid is not authorized")


def create_pipe_endpoint(**kwargs):
    """The Windows endpoint, imported only when a Windows process actually starts a server."""
    from .windows_ipc import NamedPipeEndpoint

    return NamedPipeEndpoint(**kwargs)

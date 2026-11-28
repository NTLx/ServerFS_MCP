"""Windows local IPC identity and endpoint helpers."""

from __future__ import annotations

import hashlib

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

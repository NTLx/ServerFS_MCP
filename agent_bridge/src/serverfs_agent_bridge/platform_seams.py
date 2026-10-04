"""The platform seams of §3 and what each platform provides behind them.

Phase A made the Bridge core *importable* on Windows by failing every seam closed. Phase B
implements three of the four Windows twins, so they are no longer guards:

- ``LOCAL_IPC`` — AF_UNIX on Linux, a byte Named Pipe pool on Windows (§4, ``windows_ipc``);
- ``PEER_IDENTITY`` — ``SO_PEERCRED`` on Linux, an impersonated client SID on Windows (§4.5);
- ``PRIVATE_STATE`` — owner UID plus mode bits on Linux, owner SID plus an explicit protected
  NTFS DACL on Windows (§6.1, ``private_state``).

``WRITER_LEASE`` stays a guard until Phase C: ``flock`` has no Windows twin yet, and a Bridge
that could not lease a workdir must refuse a ``workspace-write`` task rather than run one
unleased. Everything above these seams is provider-neutral and platform-independent.
"""

from __future__ import annotations

import sys

from .errors import BridgeError

LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform == "win32"

LOCAL_IPC = "local IPC"
PEER_IDENTITY = "peer identity"
WRITER_LEASE = "the flock-backed writer lease"
PRIVATE_STATE = "private-state authorization"

#: Seams whose Windows twin is not implemented yet. Phase C removes the writer lease from
#: this list; nothing else may join it, because a seam on the list is a missing feature.
UNIMPLEMENTED_ON_WINDOWS = {WRITER_LEASE: "Phase C"}

__all__ = [
    "LINUX",
    "WINDOWS",
    "LOCAL_IPC",
    "PEER_IDENTITY",
    "PRIVATE_STATE",
    "WRITER_LEASE",
    "require_linux_seam",
]


def require_linux_seam(seam: str) -> None:
    """Fail closed when a caller asks a Windows process for an unimplemented seam."""
    if LINUX:
        return
    phase = UNIMPLEMENTED_ON_WINDOWS.get(seam)
    detail = f"; the Windows twin is scheduled for {phase}" if phase else ""
    raise BridgeError(
        "BRIDGE_PLATFORM_UNSUPPORTED",
        f"{seam} is not available in this Bridge process{detail}",
    )

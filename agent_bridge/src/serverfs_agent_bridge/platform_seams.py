"""The platform seams of §3 and what they require from the running kernel.

v0.11 Phase A makes the Bridge core *importable* on Windows without giving
Windows an implementation of any seam: a component that would reach a
Linux-specific mechanism reports that this process cannot provide it instead of
raising `AttributeError` from inside a primitive call. The Windows twins are
scheduled, not invented here — Named Pipe plus client SID (Phase B), the
LockFileEx writer lease (Phase C), NTFS DACL private state (Phase B/D) and Job
Object containment (Phase D). Everything above these seams stays
provider-neutral and unchanged.
"""

from __future__ import annotations

import sys

from .errors import BridgeError

LINUX = sys.platform.startswith("linux")

LOCAL_IPC = "local IPC over a Unix-domain socket"
PEER_IDENTITY = "peer identity from SO_PEERCRED"
WRITER_LEASE = "the flock-backed writer lease"
PRIVATE_STATE = "private-state authorization by owner UID and mode bits"

__all__ = [
    "LINUX",
    "LOCAL_IPC",
    "PEER_IDENTITY",
    "PRIVATE_STATE",
    "WRITER_LEASE",
    "require_linux_seam",
]


def require_linux_seam(seam: str) -> None:
    """Fail closed when a caller asks this process for a Linux-only mechanism."""
    if LINUX:
        return
    raise BridgeError(
        "BRIDGE_PLATFORM_UNSUPPORTED",
        f"{seam} is not available in this Bridge process",
    )

"""The platform seams of §3 and what each platform provides behind them.

Phase A made the Bridge core *importable* on Windows by failing every seam closed. Phase B and
Phase C implement all four Windows twins, so none of them is a guard anymore:

- ``LOCAL_IPC`` — AF_UNIX on Linux, a byte Named Pipe pool on Windows (§4, ``windows_ipc``);
- ``PEER_IDENTITY`` — ``SO_PEERCRED`` on Linux, an impersonated client SID on Windows (§4.5);
- ``PRIVATE_STATE`` — owner UID plus mode bits on Linux, owner SID plus an explicit protected
  NTFS DACL on Windows (§6.1, ``private_state``).

``WRITER_LEASE`` joined them in Phase C: ``flock`` on Linux, an exclusive non-blocking
``LockFileEx`` over the same Bridge-created artifact on Windows (§5.5, ``windows_lease``). A Bridge
that cannot lease a workdir still refuses a ``workspace-write`` task rather than running one
unleased, and every code path that needs a lease goes through ``leases.LeaseManager`` so that rule
cannot be bypassed. Everything above these seams is provider-neutral and platform-independent.
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

#: Seams whose Windows twin is not implemented yet. Empty after Phase C: a seam on this list is a
#: missing feature, and only a demonstrated defect may put one back.
UNIMPLEMENTED_ON_WINDOWS: dict[str, str] = {}

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

"""Read-only Windows kernel proof of a Bridge v2 named Job's pipe server PID.

A Core opens the existing named Job with QUERY-only access. It never creates,
configures, assigns a process to, closes the owner handle or kills a Job.
The HMAC Core grant is a separate, mandatory authorization boundary.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable

from .bridge_resources import WindowsBridgeResources

_JOB_NAME = re.compile(r"Global\\serverfs-agent-bridge-job-v25-[0-9a-f]{32}\Z")
_QUERY_JOB = 0x0004


class BridgePeerJobError(ValueError):
    """No OS-backed proof of this specific independent Bridge generation."""


def job_member_read_only(job_name: str, process_id: int) -> bool:
    """Ask the Windows kernel if the peer belongs to an existing named Job.

    False when the owner has not started yet; exceptions also fail closed in
    the Core's existing AgentBridgeClient._exchange_pipe transport.
    """
    if (
        sys.platform != "win32"
        or not isinstance(job_name, str)
        or not _JOB_NAME.fullmatch(job_name)
        or type(process_id) is not int
        or process_id <= 0
    ):
        raise BridgePeerJobError("independent Bridge owner identity is invalid")
    import ctypes
    from ctypes import wintypes

    from serverfs_mcp.windows_job import _process_in_job

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenJobObjectW(_QUERY_JOB, False, job_name)
    if not handle:
        return False
    try:
        return _process_in_job(int(handle), process_id)
    finally:
        kernel.CloseHandle(handle)


def make_bridge_peer_validator(
    *,
    bridge_id: str,
    user_sid: str,
    job_member: Callable[[str, int], bool] = job_member_read_only,
) -> Callable[[int], bool]:
    """Bind the validator to an OS/SID-derived name, never client JSON."""
    from serverfs_mcp.instance_identity import DEFAULT_INSTANCE_ID

    if bridge_id == DEFAULT_INSTANCE_ID:
        raise BridgePeerJobError("protocol-v2 peer cannot select the legacy Bridge")
    resources = WindowsBridgeResources(user_sid, bridge_id)
    job_name = resources.job_name
    if not callable(job_member):
        raise BridgePeerJobError("kernel Job membership verifier is unavailable")

    def validate(process_id: int) -> bool:
        if type(process_id) is not int or process_id <= 0:
            return False
        return job_member(job_name, process_id) is True

    return validate

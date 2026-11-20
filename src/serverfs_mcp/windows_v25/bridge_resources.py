"""Windows v0.25 native Bridge owner resource identity (no activation).

The existing per-SID Bridge owns its current v1 pipe, Job and lifecycle
artifact without renaming. A *future* independently owned Bridge receives a
distinct v25 identity derived from the authenticated user SID and a validated
bridge_id. This is only a naming contract; shared-Bridge authorization,
provider containment and OS ownership must be implemented and proven before
any non-default Bridge may be launched.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from serverfs_mcp.instance_identity import DEFAULT_INSTANCE_ID, valid_id

_SID = re.compile(r"S-1-(?:[0-9]+-)+[0-9]+\Z")
_PIPE_ROOT = "\\\\.\\pipe\\"
_V1_PIPE_PREFIX = _PIPE_ROOT + "serverfs-agent-bridge-v1-"
_V25_PIPE_PREFIX = _PIPE_ROOT + "serverfs-agent-bridge-v25-"


class BridgeIdentityError(ValueError):
    """Bridge resource ownership could not be derived safely."""


@dataclass(frozen=True)
class WindowsBridgeResources:
    user_sid: str
    bridge_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.user_sid, str) or not _SID.fullmatch(self.user_sid):
            raise BridgeIdentityError("Windows Bridge owner requires a canonical user SID")
        valid_id(self.bridge_id)

    @property
    def _legacy_pipe(self) -> str:
        digest = hashlib.sha256(self.user_sid.upper().encode("ascii")).hexdigest()[:16]
        return _V1_PIPE_PREFIX + digest

    @property
    def _legacy_job_digest(self) -> str:
        return hashlib.sha256(self._legacy_pipe.encode("utf-8")).hexdigest()[:32]

    @property
    def _v25_digest(self) -> str:
        material = (
            b"serverfs-windows-v25-bridge\x00"
            + self.user_sid.upper().encode("ascii")
            + b"\x00"
            + self.bridge_id.encode("ascii")
        )
        return hashlib.sha256(material).hexdigest()

    @property
    def pipe_name(self) -> str:
        if self.bridge_id == DEFAULT_INSTANCE_ID:
            return self._legacy_pipe
        return _V25_PIPE_PREFIX + self._v25_digest[:16]

    @property
    def job_name(self) -> str:
        if self.bridge_id == DEFAULT_INSTANCE_ID:
            return "Global\\serverfs-agent-bridge-job-v1-" + self._legacy_job_digest
        return "Global\\serverfs-agent-bridge-job-v25-" + self._v25_digest[:32]

    @property
    def lifecycle_artifact_name(self) -> str:
        # Existing LifecycleLease(root=...) can be reused for distinct private
        # roots; for non-default Bridges the root MUST come from OS trusted
        # LocalAppData, never an inherited environment variable.
        digest = (
            self._legacy_job_digest
            if self.bridge_id == DEFAULT_INSTANCE_ID
            else self._v25_digest[:32]
        )
        return "serverfs-agent-lifecycle-" + digest + ".lifecycle"

    @property
    def private_data_relative(self) -> Path:
        if self.bridge_id == DEFAULT_INSTANCE_ID:
            return Path("agent-bridge")
        return Path("v25") / "instances" / self.bridge_id / "agent-bridge"

    @property
    def lifecycle_root_relative(self) -> Path:
        if self.bridge_id == DEFAULT_INSTANCE_ID:
            return Path("agent-lifecycle")
        return Path("v25") / "instances" / self.bridge_id / "agent-lifecycle"

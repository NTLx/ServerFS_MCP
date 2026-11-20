"""Windows v25: reject overlapping writable roots of independent Bridges.

This pure preflight compares already-canonical host roots; it is not an
authorization system or an installed-Bridge inventory. A future Bridge
installer MUST resolve each path securely on the host, verify owner policy,
and apply this rule atomically with ownership registration before start.
"""

from __future__ import annotations

import ntpath
from dataclasses import dataclass

from serverfs_mcp.instance_identity import valid_id


class WorkdirOwnershipError(ValueError):
    """Unsafe independent Bridge workdir ownership or malformed root."""


def _canonical_parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not ntpath.isabs(path):
        raise WorkdirOwnershipError("Bridge workdir must have an absolute Windows path")
    raw_parts = path.replace("/", "\\").split("\\")
    if any(value in (".", "..") for value in raw_parts):
        raise WorkdirOwnershipError("Bridge workdir must already be canonicalized")
    normalized = ntpath.normcase(ntpath.normpath(path))
    if normalized.startswith("\\\\?\\") or normalized.startswith("\\\\.\\"):
        raise WorkdirOwnershipError("unsupported Bridge workdir device path")
    # Windows case folding and path-component boundaries, not string prefix.
    return tuple(part for part in normalized.replace("/", "\\").split("\\") if part)


@dataclass(frozen=True)
class WorkdirClaim:
    bridge_id: str
    root: str
    writable: bool

    def __post_init__(self) -> None:
        valid_id(self.bridge_id)
        _canonical_parts(self.root)
        if type(self.writable) is not bool:
            raise WorkdirOwnershipError("Bridge workdir writable flag must be boolean")

    @property
    def parts(self) -> tuple[str, ...]:
        return _canonical_parts(self.root)


def _overlap(a: WorkdirClaim, b: WorkdirClaim) -> bool:
    a_parts, b_parts = a.parts, b.parts
    length = min(len(a_parts), len(b_parts))
    return a_parts[:length] == b_parts[:length]


def assert_independent_bridge_roots(
    proposed: tuple[WorkdirClaim, ...],
    existing: tuple[WorkdirClaim, ...],
) -> None:
    """Reject writable cross-Bridge canonical root intersection.

    Same Bridge owns its own policy and its own single writer-lease universe.
    Different Bridges may overlap ONLY when both claims are read-only.
    """
    for index, a in enumerate(proposed):
        # Check new claims against each other as well as installed owners.
        for b in (*proposed[index + 1 :], *existing):
            if a.bridge_id == b.bridge_id:
                continue
            if (a.writable or b.writable) and _overlap(a, b):
                raise WorkdirOwnershipError(
                    "independent Bridges cannot overlap on a writable workdir"
                )

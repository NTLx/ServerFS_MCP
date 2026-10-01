"""Platform-neutral coded error contract used by the product layer.

These exception classes carry the agent-facing ``code``/``message`` pair and
nothing else: no file descriptors, no errno, no platform objects. The tool
layer maps them to ``CODE: message`` ToolErrors, so it must be able to
recognize them without importing any Linux-only kernel module (§11 import
safety). Platform kernels raise them; the mutation kernel
(``mutations.py``) defines its subclasses on top of ``MutationError``.
"""

from __future__ import annotations


class MutationError(Exception):
    """Base class for anticipated mutation failures (CODE: message)."""

    code = "MUTATION_FAILED"
    message = "mutation failed"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class AgentLeaseError(Exception):
    code = "AGENT_LOCK_UNAVAILABLE"
    message = "shared Agent workdir lease is unavailable"


class WorkdirBusyError(AgentLeaseError):
    code = "WORKDIR_BUSY"
    message = "workdir is busy with an active Agent task"


class WorkdirRecoveryRequiredError(AgentLeaseError):
    code = "WORKDIR_RECOVERY_REQUIRED"
    message = "workdir has unresolved Agent recovery state"


__all__ = [
    "AgentLeaseError",
    "MutationError",
    "WorkdirBusyError",
    "WorkdirRecoveryRequiredError",
]

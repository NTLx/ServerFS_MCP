"""Provider-neutral models for Agent Bridge Phase A."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_QUESTION = "waiting_for_question"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.INTERRUPTED,
    }
)


class AgentProfile(StrEnum):
    REVIEW = "review"
    WORKSPACE_WRITE = "workspace-write"


class AgentMode(StrEnum):
    DISABLED = "disabled"
    REVIEW = "review"
    WORKSPACE_WRITE = "workspace-write"


# The v0.3 configuration validates provider names centrally. Phase A supplies
# the deterministic ``fake`` runtime; Phase B adds Codex and Phase C adds Claude.
KNOWN_RUNTIME_NAMES = frozenset({"fake", "codex", "claude"})


class RequestKind(StrEnum):
    APPROVAL = "approval"
    QUESTION = "question"


class RequestStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    STALE = "stale"


class ApprovalDecision(StrEnum):
    APPROVE_ONCE = "approve_once"
    APPROVE_SESSION = "approve_session"
    DENY = "deny"
    CANCEL_TASK = "cancel_task"


@dataclass(frozen=True)
class RuntimeInfo:
    name: str
    available: bool
    version: str | None = None
    persistent_session: bool = False
    live_steer: bool = False
    interactive_approval: bool = False
    interactive_question: bool = False
    in_flight_recovery: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    runtime: str
    workdir_alias: str
    workdir_slot: int
    relative_cwd: str
    profile: str
    status: str
    created_at: str
    started_at: str | None
    updated_at: str
    completed_at: str | None
    continue_from_task_id: str | None
    native_session_id: str | None
    native_turn_id: str | None
    final_response: str | None
    error_code: str | None
    error_message: str | None
    pending_request_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PendingRequest:
    request_id: str
    task_id: str
    kind: str
    status: str
    payload: dict[str, Any]
    created_at: str
    resolved_at: str | None
    resolution: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BridgeEvent:
    event_id: int
    task_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

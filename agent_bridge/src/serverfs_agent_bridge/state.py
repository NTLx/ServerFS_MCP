"""Task-state transition rules."""

from __future__ import annotations

from .errors import BridgeError
from .models import TaskStatus

_ALLOWED: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.STARTING, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED}
    ),
    TaskStatus.STARTING: frozenset(
        {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.WAITING_FOR_QUESTION,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.WAITING_FOR_APPROVAL: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED}
    ),
    TaskStatus.WAITING_FOR_QUESTION: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED}
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.INTERRUPTED: frozenset(),
}


def can_transition(current: TaskStatus | str, target: TaskStatus | str) -> bool:
    return TaskStatus(target) in _ALLOWED[TaskStatus(current)]


def validate_transition(current: TaskStatus | str, target: TaskStatus | str) -> None:
    if not can_transition(current, target):
        raise BridgeError(
            "INVALID_TASK_TRANSITION",
            "task cannot transition from "
            f"{TaskStatus(current).value} to {TaskStatus(target).value}",
        )

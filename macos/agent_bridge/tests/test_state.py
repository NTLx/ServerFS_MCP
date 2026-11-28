from __future__ import annotations

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.models import TaskStatus
from serverfs_agent_bridge.state import can_transition, validate_transition


def test_expected_transitions() -> None:
    assert can_transition(TaskStatus.QUEUED, TaskStatus.STARTING)
    assert can_transition(TaskStatus.RUNNING, TaskStatus.WAITING_FOR_APPROVAL)
    assert can_transition(TaskStatus.WAITING_FOR_APPROVAL, TaskStatus.RUNNING)
    assert can_transition(TaskStatus.RUNNING, TaskStatus.SUCCEEDED)


def test_terminal_task_cannot_restart() -> None:
    with pytest.raises(BridgeError, match="cannot transition"):
        validate_transition(TaskStatus.SUCCEEDED, TaskStatus.RUNNING)

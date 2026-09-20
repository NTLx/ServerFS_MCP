"""Agent runtime adapters."""

from .base import AdapterResult, AgentAdapter, TaskContext
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .fake import FakeAdapter

__all__ = [
    "AdapterResult",
    "AgentAdapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "FakeAdapter",
    "TaskContext",
]

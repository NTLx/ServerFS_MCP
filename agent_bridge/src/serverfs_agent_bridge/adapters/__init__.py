"""Agent runtime adapters."""

from .base import AdapterResult, AgentAdapter, TaskContext
from .codex import CodexAdapter
from .fake import FakeAdapter

__all__ = ["AdapterResult", "AgentAdapter", "CodexAdapter", "FakeAdapter", "TaskContext"]

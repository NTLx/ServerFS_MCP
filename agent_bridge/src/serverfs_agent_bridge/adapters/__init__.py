"""Agent runtime adapters."""

from .base import AdapterResult, AgentAdapter, TaskContext
from .fake import FakeAdapter

__all__ = ["AdapterResult", "AgentAdapter", "FakeAdapter", "TaskContext"]

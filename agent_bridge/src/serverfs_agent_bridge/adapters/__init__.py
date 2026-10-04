"""Agent runtime adapters.

Importing this package must not import a provider SDK: the Bridge core
(protocol, service, store, leases) and the Fake runtime have to stay loadable on
a host where only one provider is installed, and an SDK that is missing must
produce an explicit error for the runtime that needs it rather than breaking
every Bridge import. The SDK-backed adapters are therefore resolved on first
attribute access (§A3).
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .base import AdapterResult, AgentAdapter, TaskContext
from .fake import FakeAdapter

if TYPE_CHECKING:
    from .claude import ClaudeAdapter
    from .codex import CodexAdapter
    from .qoder import QoderAdapter

_RUNTIME_ADAPTERS = {
    "ClaudeAdapter": (".claude", "claude-agent-sdk"),
    "CodexAdapter": (".codex", "Codex App Server transport"),
    "QoderAdapter": (".qoder", "qoder-agent-sdk"),
}

__all__ = [
    "AdapterResult",
    "AgentAdapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "FakeAdapter",
    "QoderAdapter",
    "TaskContext",
]


def __getattr__(name: str) -> Any:
    target = _RUNTIME_ADAPTERS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, requirement = target
    try:
        module = import_module(module_name, __name__)
    except ImportError as exc:
        raise ImportError(
            f"{name} requires {requirement}, which is not importable in this Bridge process"
        ) from exc
    adapter = getattr(module, name)
    globals()[name] = adapter
    return adapter


def __dir__() -> list[str]:
    return sorted({*globals(), *_RUNTIME_ADAPTERS})

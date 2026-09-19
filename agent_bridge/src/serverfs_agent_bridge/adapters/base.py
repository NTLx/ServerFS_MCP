"""Provider-neutral adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import RuntimeInfo

EmitEvent = Callable[[str, dict[str, Any]], Awaitable[None]]
RequestApproval = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
AskQuestion = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
AbandonInteraction = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class TaskContext:
    task_id: str
    workdir: str
    workdir_root: Path
    cwd: Path
    profile: str
    prompt: str
    continue_native_session_id: str | None
    emit_event: EmitEvent
    request_approval: RequestApproval
    ask_question: AskQuestion
    abandon_interaction: AbandonInteraction


@dataclass(frozen=True)
class AdapterResult:
    final_response: str
    native_session_id: str | None = None
    native_turn_id: str | None = None


class AgentAdapter(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def probe(self) -> RuntimeInfo:
        raise NotImplementedError

    @abstractmethod
    async def run_task(self, context: TaskContext) -> AdapterResult:
        raise NotImplementedError

    async def continue_task(self, context: TaskContext) -> AdapterResult:
        """Run a new turn against the native session in the task context."""
        return await self.run_task(context)

    async def get_state(self, task_id: str) -> str | None:
        raise NotImplementedError

    async def respond_approval(
        self, task_id: str, request_id: str, resolution: dict[str, Any]
    ) -> None:
        raise NotImplementedError

    async def answer_question(
        self, task_id: str, request_id: str, resolution: dict[str, Any]
    ) -> None:
        raise NotImplementedError

    async def reconcile(self) -> None:
        return None

    async def send_message(self, task_id: str, message: str) -> None:
        raise NotImplementedError

    async def cancel(self, task_id: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        return None

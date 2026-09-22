"""Deterministic fake adapter used by Phase A tests only."""

from __future__ import annotations

import asyncio
import json

from ..models import RuntimeInfo
from .base import AdapterResult, AgentAdapter, TaskContext


class FakeAdapter(AgentAdapter):
    def __init__(self) -> None:
        self._cancelled: set[str] = set()
        self.messages: dict[str, list[str]] = {}
        self._message_events: dict[str, asyncio.Event] = {}

    @property
    def name(self) -> str:
        return "fake"

    async def probe(self) -> RuntimeInfo:
        return RuntimeInfo(
            name=self.name,
            available=True,
            version="phase-a",
            persistent_session=True,
            live_steer=True,
            interactive_approval=True,
            interactive_question=True,
            in_flight_recovery="none",
        )

    async def run_task(self, context: TaskContext) -> AdapterResult:
        await context.emit_event("turn.started", {"runtime": self.name})
        self.messages.setdefault(context.task_id, [])

        if context.prompt.startswith("approval-twice:"):
            command = context.prompt.removeprefix("approval-twice:").strip() or "echo test"
            decisions: list[str] = []
            approval_payload = {
                "category": "command",
                "title": "Fake command approval",
                "command_display": command,
                "available_decisions": [
                    "approve_once",
                    "approve_session",
                    "deny",
                    "cancel_task",
                ],
            }
            for _ in range(2):
                resolution = await context.request_approval(dict(approval_payload))
                if resolution["decision"] == "cancel_task":
                    raise asyncio.CancelledError
                decisions.append(resolution["decision"])
                await context.emit_event("approval.observed", resolution)
            response = f"approvals={','.join(decisions)}"
        elif context.prompt.startswith("approval:"):
            command = context.prompt.removeprefix("approval:").strip() or "echo test"
            resolution = await context.request_approval(
                {
                    "category": "command",
                    "title": "Fake command approval",
                    "command_display": command,
                    "available_decisions": [
                        "approve_once",
                        "approve_session",
                        "deny",
                        "cancel_task",
                    ],
                }
            )
            if resolution["decision"] == "cancel_task":
                raise asyncio.CancelledError
            await context.emit_event("approval.observed", resolution)
            response = f"approval={resolution['decision']}"
        elif context.prompt.startswith("question:"):
            prompt = context.prompt.removeprefix("question:").strip() or "Choose"
            answer = await context.ask_question(
                {
                    "questions": [
                        {
                            "question_id": "q1",
                            "prompt": prompt,
                            "options": [
                                {"option_id": "a", "label": "A"},
                                {"option_id": "b", "label": "B"},
                            ],
                            "multi_select": False,
                            "allow_free_text": True,
                        }
                    ]
                }
            )
            response = json.dumps(answer, ensure_ascii=False, sort_keys=True)
        elif context.prompt.startswith("wait:"):
            while context.task_id not in self._cancelled:
                await asyncio.sleep(0.01)
            raise asyncio.CancelledError
        elif context.prompt.startswith("steer:"):
            event = self._message_events.setdefault(context.task_id, asyncio.Event())
            if self.messages[context.task_id]:
                event.set()
            await event.wait()
            if context.task_id in self._cancelled:
                raise asyncio.CancelledError
            response = f"steered={self.messages[context.task_id][-1]}"
        else:
            response = context.prompt.removeprefix("complete:").strip()

        if context.task_id in self._cancelled:
            raise asyncio.CancelledError

        await context.emit_event("agent.message", {"text": response})
        return AdapterResult(
            final_response=response,
            native_session_id=context.continue_native_session_id
            or f"fake-session-{context.task_id}",
            native_turn_id=f"fake-turn-{context.task_id}",
        )

    async def send_message(self, task_id: str, message: str) -> None:
        self.messages.setdefault(task_id, []).append(message)
        event = self._message_events.get(task_id)
        if event is not None:
            event.set()

    async def cancel(self, task_id: str) -> None:
        self._cancelled.add(task_id)
        event = self._message_events.get(task_id)
        if event is not None:
            event.set()

    async def close(self) -> None:
        self._cancelled.clear()
        self.messages.clear()
        self._message_events.clear()

"""Qoder Agent SDK adapter for the ServerFS Agent Bridge.

The adapter delegates to the existing system Qoder CLI installation and login
through Qoder's official Python Agent SDK. ServerFS owns task lifecycle,
workdir leasing and remote human interaction; Qoder keeps its native settings,
tools, MCPs, skills, plugins and model selection.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qoder_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    QoderAgentOptions,
    QoderSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    qodercli_auth,
)

from ..config import QoderSettings
from ..errors import BridgeError
from ..models import AgentProfile, ReconciliationStatus, RuntimeInfo, TaskRecord
from .base import AdapterResult, AgentAdapter, ReconcileResult, TaskContext

_ClientFactory = Callable[[QoderAgentOptions], QoderSDKClient]


@dataclass
class _ActiveQoderTask:
    context: TaskContext
    client: QoderSDKClient
    client_ready: asyncio.Event = field(default_factory=asyncio.Event)
    latest_text: str = ""
    session_id: str | None = None
    pending_interactions: int = 0


class QoderAdapter(AgentAdapter):
    def __init__(
        self,
        settings: QoderSettings,
        *,
        client_factory: _ClientFactory = QoderSDKClient,
    ) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._active: dict[str, _ActiveQoderTask] = {}
        self._active_lock = asyncio.Lock()
        self._cancel_requested: set[str] = set()
        self._locally_stopped: set[str] = set()
        self._closed = False

    @property
    def name(self) -> str:
        return "qoder"

    async def probe(self) -> RuntimeInfo:
        if not self.settings.enabled or self._closed:
            return self._runtime_info(available=False)
        binary = _resolve_cli(self.settings.qoder_bin)
        if binary is None:
            return self._runtime_info(available=False)

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self.settings.probe_timeout_seconds,
            )
        except TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return self._runtime_info(available=False)
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except OSError:
            return self._runtime_info(available=False)

        if process.returncode != 0:
            return self._runtime_info(available=False)
        version = stdout.decode("utf-8", errors="replace").strip() or None
        return self._runtime_info(available=True, version=version)

    async def list_models(self) -> dict[str, Any]:
        if self._closed or not self.settings.enabled:
            return {
                "runtime": self.name,
                "status": "unavailable",
                "scope": "none",
                "source": "qoder_agent_sdk",
                "models": [],
            }
        cli_path = _resolve_cli(self.settings.qoder_bin)
        if cli_path is None:
            raise BridgeError("AGENT_RUNTIME_UNAVAILABLE", "configured Qoder CLI is unavailable")
        options = QoderAgentOptions(
            auth=qodercli_auth(),
            cwd=Path.home(),
            cli_path=cli_path,
            setting_sources=["user"],
        )
        client = self._client_factory(options)
        try:
            async with asyncio.timeout(max(10.0, self.settings.probe_timeout_seconds * 2)):
                await client.connect()
                values = await client.get_available_models()
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        if not isinstance(values, list) or not values:
            raise BridgeError("AGENT_PROVIDER_ERROR", "Qoder returned no available models")
        models: list[dict[str, Any]] = []
        for value in values[:64]:
            normalized = _normalize_qoder_model(value)
            if normalized is not None:
                models.append(normalized)
        if not models:
            raise BridgeError("AGENT_PROVIDER_ERROR", "Qoder returned an invalid model catalog")
        return {
            "runtime": self.name,
            "status": "ok",
            "scope": "current_account",
            "source": "qoder_agent_sdk",
            "models": models,
        }

    async def run_task(self, context: TaskContext) -> AdapterResult:
        return await self._run(context, resume=False)

    async def continue_task(self, context: TaskContext) -> AdapterResult:
        if not context.continue_native_session_id:
            raise BridgeError(
                "AGENT_SESSION_NOT_RESUMABLE",
                "Qoder continuation requires a native session id",
            )
        return await self._run(context, resume=True)

    async def get_state(self, task_id: str) -> str | None:
        return "running" if task_id in self._active else None

    async def send_message(self, task_id: str, message: str) -> None:
        # The installed SDK/CLI probe proves priority="now" produces an
        # intermediate error Result before the steered success Result. The
        # current Bridge task contract terminates on the first Result, so live
        # steering remains disabled rather than emulating multi-Result turns.
        if task_id not in self._active:
            raise BridgeError("AGENT_TASK_NOT_ACTIVE", "Qoder task is not active")
        raise BridgeError(
            "AGENT_PROVIDER_ERROR",
            (
                "Qoder live steer is disabled because native priority=now "
                "produces multiple Result boundaries"
            ),
        )

    async def cancel(self, task_id: str) -> None:
        active = self._active.get(task_id)
        if active is None:
            return
        self._cancel_requested.add(task_id)
        if not active.client_ready.is_set():
            return
        try:
            await active.client.interrupt()
        except Exception:
            # Bridge-side cancellation remains authoritative.
            return

    async def reconcile_task(self, task: TaskRecord) -> ReconcileResult:
        if task.task_id in self._locally_stopped:
            self._locally_stopped.discard(task.task_id)
            return ReconcileResult(
                status=(
                    ReconciliationStatus.SESSION_RESUMABLE
                    if task.native_session_id is not None
                    else ReconciliationStatus.NOT_RECOVERABLE
                ),
                provider_active=False,
                detail="local Qoder SDK client disconnected and the subprocess is stopped",
            )
        if task.native_session_id is None:
            return ReconcileResult(
                status=ReconciliationStatus.NOT_RECOVERABLE,
                provider_active=None,
                detail="task has no persisted Qoder session id; prior process state is unknown",
            )
        return ReconcileResult(
            status=ReconciliationStatus.SESSION_RESUMABLE,
            provider_active=None,
            detail=(
                "Qoder session is resumable by a new task, but that does not prove "
                "the prior in-flight subprocess has stopped"
            ),
        )

    async def close(self) -> None:
        self._closed = True
        active = list(self._active.values())
        for state in active:
            try:
                if state.client_ready.is_set():
                    await state.client.interrupt()
            except Exception:
                pass

    async def _run(self, context: TaskContext, *, resume: bool) -> AdapterResult:
        if self._closed:
            raise BridgeError("AGENT_RUNTIME_UNAVAILABLE", "Qoder adapter is closed")
        if not self.settings.enabled:
            raise BridgeError("AGENT_RUNTIME_UNAVAILABLE", "Qoder runtime is disabled")
        if context.profile != AgentProfile.WORKSPACE_WRITE.value:
            raise BridgeError(
                "AGENT_PROFILE_NOT_ALLOWED",
                "Qoder native mode requires workspace-write so the Bridge holds the workdir lease",
            )

        cli_path = _resolve_cli(self.settings.qoder_bin)
        if cli_path is None:
            raise BridgeError(
                "AGENT_RUNTIME_UNAVAILABLE",
                "configured Qoder CLI is unavailable",
            )

        async def can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            permission_context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            return await self._can_use_tool(
                context,
                tool_name,
                tool_input,
                permission_context,
            )

        options = QoderAgentOptions(
            auth=qodercli_auth(),
            cwd=context.cwd,
            cli_path=cli_path,
            setting_sources=["user", "project", "local"],
            can_use_tool=can_use_tool,
            resume=context.continue_native_session_id if resume else None,
            model=context.requested_model,
        )
        client = self._client_factory(options)
        active = _ActiveQoderTask(context=context, client=client)

        async with self._active_lock:
            if context.task_id in self._active:
                raise BridgeError("AGENT_PROVIDER_ERROR", "Qoder task is already active")
            self._active[context.task_id] = active

        record_local_stop = False
        try:
            if context.continue_native_session_id is not None:
                active.session_id = context.continue_native_session_id
                await context.record_native_ids(context.continue_native_session_id, None)

            await client.connect(context.prompt)
            active.client_ready.set()
            if context.task_id in self._cancel_requested:
                await client.interrupt()

            result = await self._receive_result(active)
            if result.is_error:
                detail = _result_error_detail(result)
                raise BridgeError(
                    "AGENT_PROVIDER_ERROR",
                    detail or "Qoder returned an error result",
                )

            final = result.result if isinstance(result.result, str) else active.latest_text
            if not isinstance(final, str):
                final = ""
            native_turn_id = result.uuid if isinstance(result.uuid, str) else None
            active.session_id = result.session_id
            await context.record_native_ids(result.session_id, native_turn_id)
            return AdapterResult(
                final_response=final,
                native_session_id=result.session_id,
                native_turn_id=native_turn_id,
            )
        except asyncio.CancelledError:
            record_local_stop = True
            raise
        except BridgeError:
            record_local_stop = True
            raise
        except Exception as exc:
            record_local_stop = True
            raise BridgeError("AGENT_PROVIDER_ERROR", "Qoder task failed") from exc
        finally:
            active.client_ready.set()
            cancel_requested = context.task_id in self._cancel_requested
            disconnect_succeeded = False
            try:
                await client.disconnect()
                disconnect_succeeded = True
            except Exception:
                pass
            async with self._active_lock:
                self._active.pop(context.task_id, None)
            self._cancel_requested.discard(context.task_id)
            if record_local_stop or (cancel_requested and disconnect_succeeded):
                self._locally_stopped.add(context.task_id)

    async def _receive_result(self, active: _ActiveQoderTask) -> ResultMessage:
        iterator = active.client.receive_response().__aiter__()
        while True:
            try:
                message = await self._next_message(active, iterator)
            except StopAsyncIteration as exc:
                raise BridgeError(
                    "AGENT_PROVIDER_ERROR",
                    "Qoder response ended without a ResultMessage",
                ) from exc

            if isinstance(message, SystemMessage):
                await self._handle_system_message(active, message)
                continue
            if isinstance(message, AssistantMessage):
                await self._handle_assistant_message(active, message)
                continue
            if isinstance(message, ResultMessage):
                return message

    async def _next_message(self, active: _ActiveQoderTask, iterator: Any) -> Any:
        async def receive_one() -> Any:
            return await anext(iterator)

        pending = asyncio.create_task(receive_one())
        timeout = self.settings.event_idle_timeout_seconds
        try:
            if timeout is None:
                return await pending
            while True:
                done, _ = await asyncio.wait({pending}, timeout=timeout)
                if done:
                    return await pending
                if active.pending_interactions > 0:
                    continue
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                raise BridgeError(
                    "AGENT_PROVIDER_DISCONNECTED",
                    "Qoder produced no events before the idle timeout",
                )
        except asyncio.CancelledError:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            raise

    async def _handle_system_message(
        self,
        active: _ActiveQoderTask,
        message: SystemMessage,
    ) -> None:
        if message.subtype != "init" or not isinstance(message.data, dict):
            return
        session_id = message.data.get("session_id")
        if not isinstance(session_id, str) or not session_id or session_id == active.session_id:
            return
        active.session_id = session_id
        await active.context.record_native_ids(session_id, None)

    async def _handle_assistant_message(
        self,
        active: _ActiveQoderTask,
        message: AssistantMessage,
    ) -> None:
        if isinstance(message.session_id, str) and message.session_id:
            if message.session_id != active.session_id:
                active.session_id = message.session_id
                await active.context.record_native_ids(message.session_id, None)

        for block in message.content:
            if isinstance(block, TextBlock):
                active.latest_text = block.text
                await active.context.emit_event("agent.message", {"text": block.text})
                continue
            if isinstance(block, ToolUseBlock):
                payload: dict[str, Any] = {
                    "tool": block.name,
                    "tool_use_id": block.id,
                }
                if isinstance(block.input, dict):
                    payload["input"] = block.input
                if block.name == "Bash":
                    command = block.input.get("command") if isinstance(block.input, dict) else None
                    if isinstance(command, str):
                        await active.context.emit_event(
                            "command.started",
                            {"command": command, "tool_use_id": block.id},
                        )
                        continue
                if block.name in {"Write", "Edit", "NotebookEdit"}:
                    await active.context.emit_event("file_change.started", payload)
                else:
                    await active.context.emit_event("tool.started", payload)

    async def _can_use_tool(
        self,
        context: TaskContext,
        tool_name: str,
        tool_input: dict[str, Any],
        permission_context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        try:
            if tool_name == "AskUserQuestion":
                return await self._ask_user_question(context, tool_input)

            session_suggestions = _session_permission_suggestions(permission_context.suggestions)
            available = ["approve_once"]
            if session_suggestions:
                available.append("approve_session")
            available.extend(["deny", "cancel_task"])

            payload: dict[str, Any] = {
                "category": _permission_category(tool_name),
                "title": permission_context.title
                or permission_context.display_name
                or f"Qoder wants to use {tool_name}",
                "tool": tool_name,
                "tool_input": tool_input,
                "available_decisions": available,
            }
            reason = permission_context.decision_reason or permission_context.description
            if isinstance(reason, str) and reason:
                payload["reason"] = reason
            if isinstance(permission_context.blocked_path, str):
                payload["blocked_path"] = permission_context.blocked_path
            if tool_name == "Bash":
                command = tool_input.get("command")
                if isinstance(command, str):
                    payload["command_display"] = command

            active = self._active.get(context.task_id)
            if active is not None:
                active.pending_interactions += 1
            try:
                resolution = await context.request_approval(payload)
            finally:
                if active is not None:
                    active.pending_interactions = max(0, active.pending_interactions - 1)

            decision = resolution.get("decision")
            if decision == "approve_once":
                return PermissionResultAllow(updated_input=tool_input)
            if decision == "approve_session" and session_suggestions:
                return PermissionResultAllow(
                    updated_input=tool_input,
                    updated_permissions=session_suggestions,
                )
            if decision == "cancel_task":
                return PermissionResultDeny(
                    message="Cancelled by the remote user",
                    interrupt=True,
                )
            return PermissionResultDeny(message="Denied by the remote user", interrupt=False)
        except asyncio.CancelledError:
            await context.abandon_interaction()
            raise
        except BridgeError as exc:
            return PermissionResultDeny(message=exc.message, interrupt=False)
        except Exception:
            return PermissionResultDeny(
                message="ServerFS could not process the permission request",
                interrupt=False,
            )

    async def _ask_user_question(
        self,
        context: TaskContext,
        tool_input: dict[str, Any],
    ) -> PermissionResultAllow | PermissionResultDeny:
        questions = tool_input.get("questions")
        if not isinstance(questions, list) or not questions:
            return PermissionResultDeny(message="Qoder question payload is invalid")

        normalized: list[dict[str, Any]] = []
        question_prompts: dict[str, str] = {}
        seen_prompts: set[str] = set()
        for index, question in enumerate(questions):
            if not isinstance(question, dict):
                return PermissionResultDeny(message="Qoder question payload is invalid")
            prompt = question.get("question")
            if not isinstance(prompt, str) or not prompt:
                return PermissionResultDeny(message="Qoder question payload is invalid")
            if prompt in seen_prompts:
                return PermissionResultDeny(
                    message="Qoder question payload contains duplicate question text"
                )
            seen_prompts.add(prompt)
            question_id = f"q{index}"
            question_prompts[question_id] = prompt
            options: list[dict[str, Any]] = []
            raw_options = question.get("options", [])
            if not isinstance(raw_options, list):
                return PermissionResultDeny(message="Qoder question payload is invalid")
            for option in raw_options:
                if not isinstance(option, dict):
                    continue
                label = option.get("label")
                if not isinstance(label, str):
                    continue
                normalized_option: dict[str, Any] = {
                    "option_id": label,
                    "label": label,
                }
                description = option.get("description")
                if isinstance(description, str):
                    normalized_option["description"] = description
                preview = option.get("preview")
                if isinstance(preview, str):
                    normalized_option["preview"] = preview
                options.append(normalized_option)
            normalized.append(
                {
                    "question_id": question_id,
                    "prompt": prompt,
                    "options": options,
                    "multi_select": bool(question.get("multiSelect", False)),
                    "allow_free_text": True,
                }
            )

        active = self._active.get(context.task_id)
        if active is not None:
            active.pending_interactions += 1
        try:
            resolution = await context.ask_question({"questions": normalized})
        except asyncio.CancelledError:
            await context.abandon_interaction()
            raise
        except BridgeError as exc:
            return PermissionResultDeny(message=exc.message)
        except Exception:
            return PermissionResultDeny(message="ServerFS could not process the question")
        finally:
            if active is not None:
                active.pending_interactions = max(0, active.pending_interactions - 1)

        answers: dict[str, str] = {}
        for answer in resolution.get("answers", []):
            if not isinstance(answer, dict):
                continue
            question_id = answer.get("question_id")
            if not isinstance(question_id, str) or question_id not in question_prompts:
                continue
            values = [
                item for item in answer.get("selected_option_ids", []) if isinstance(item, str)
            ]
            text = answer.get("text")
            if isinstance(text, str) and text:
                values.append(text)
            answers[question_prompts[question_id]] = ", ".join(values)

        updated_input = dict(tool_input)
        updated_input["answers"] = answers
        return PermissionResultAllow(updated_input=updated_input)

    def _runtime_info(self, *, available: bool, version: str | None = None) -> RuntimeInfo:
        return RuntimeInfo(
            name=self.name,
            available=available,
            version=version,
            persistent_session=True,
            live_steer=False,
            interactive_approval=True,
            interactive_question=True,
            in_flight_recovery="session-resume",
            model_override=True,
            model_discovery="current_account",
        )


def _normalize_qoder_model(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        getter = value.get
    else:

        def getter(key: str, default: Any = None) -> Any:
            return getattr(value, key, default)

    model_id = getter("value")
    if not isinstance(model_id, str) or not model_id:
        return None
    display_name = getter("displayName")
    item: dict[str, Any] = {
        "id": model_id,
        "display_name": display_name if isinstance(display_name, str) else model_id,
        "enabled": getter("isEnabled") if isinstance(getter("isEnabled"), bool) else None,
        "hidden": None,
        "is_default": None,
    }
    description = getter("description")
    if isinstance(description, str):
        item["description"] = description[:2048]
    is_free = getter("isFree")
    if isinstance(is_free, bool):
        item["is_free"] = is_free
    price_factor = getter("priceFactor")
    if isinstance(price_factor, (int, float)) and not isinstance(price_factor, bool):
        item["price_factor"] = price_factor
    context = getter("context_config")
    if isinstance(context, dict):
        item["context"] = context
    thinking = getter("thinking_config")
    if isinstance(thinking, dict):
        item["reasoning"] = thinking
    is_new = getter("isNew")
    if isinstance(is_new, bool):
        item["is_new"] = is_new
    return item


def _resolve_cli(configured: str) -> str | None:
    path = Path(configured).expanduser()
    if path.is_absolute():
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(configured)


def _session_permission_suggestions(suggestions: Any) -> list[Any]:
    if not isinstance(suggestions, list):
        return []
    session_scoped: list[Any] = []
    for suggestion in suggestions:
        if isinstance(suggestion, dict):
            if suggestion.get("destination") == "session":
                session_scoped.append(suggestion)
            continue
        if getattr(suggestion, "destination", None) == "session":
            session_scoped.append(suggestion)
    return session_scoped


def _permission_category(tool_name: str) -> str:
    if tool_name == "Bash":
        return "command"
    if tool_name in {"Write", "Edit", "NotebookEdit"}:
        return "file_change"
    if tool_name in {"WebFetch", "WebSearch"}:
        return "network_permission"
    if tool_name.startswith("mcp__"):
        return "mcp_tool"
    return "other"


def _result_error_detail(result: ResultMessage) -> str | None:
    if isinstance(result.result, str) and result.result:
        return result.result
    if isinstance(result.errors, list):
        errors = [item for item in result.errors if isinstance(item, str) and item]
        if errors:
            return "; ".join(errors)
    if isinstance(result.subtype, str) and result.subtype:
        return result.subtype
    return None

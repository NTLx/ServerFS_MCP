"""Provider-neutral task orchestration for the Agent Bridge."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters.base import AgentAdapter, TaskContext
from .errors import BridgeError
from .leases import LeaseManager, WorkdirLease
from .models import (
    TERMINAL_STATUSES,
    AgentProfile,
    ApprovalDecision,
    RequestKind,
    RequestStatus,
    TaskStatus,
)
from .policy import PolicyRegistry, redact_host_path
from .preflight import TaskPreflight
from .store import TaskStore
from .util import new_id


@dataclass(frozen=True)
class BridgeLimits:
    max_prompt_bytes: int = 65_536
    max_final_response_bytes: int = 262_144
    max_event_bytes: int = 65_536
    max_interaction_bytes: int = 65_536
    max_message_bytes: int = 65_536
    max_events_per_task: int = 10_000
    max_active_tasks: int = 4

    def __post_init__(self) -> None:
        for name in (
            "max_prompt_bytes",
            "max_final_response_bytes",
            "max_event_bytes",
            "max_interaction_bytes",
            "max_message_bytes",
            "max_events_per_task",
            "max_active_tasks",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")


class BridgeService:
    def __init__(
        self,
        *,
        store: TaskStore,
        policies: PolicyRegistry,
        adapters: dict[str, AgentAdapter],
        lease_manager: LeaseManager,
        limits: BridgeLimits | None = None,
        preflight: TaskPreflight | None = None,
    ):
        self.store = store
        self.policies = policies
        self.adapters = dict(adapters)
        self.lease_manager = lease_manager
        self.limits = limits or BridgeLimits()
        self.preflight = preflight
        self._background: dict[str, asyncio.Task[None]] = {}
        self._leases: dict[str, WorkdirLease] = {}
        self._pending_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._user_cancelled: set[str] = set()
        self._submit_lock = asyncio.Lock()
        self._cancel_lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> int:
        return self.store.interrupt_nonterminal_tasks()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._background.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task_id in list(self._leases):
            self._release_lease(task_id)
            try:
                task = self.store.get_task(task_id)
            except BridgeError:
                continue
            if TaskStatus(task.status) not in TERMINAL_STATUSES:
                self.store.transition_task(
                    task_id,
                    TaskStatus.INTERRUPTED,
                    error_code="BRIDGE_SHUTDOWN",
                    error_message="bridge stopped while task was active",
                )
                self._try_append_event(
                    task_id, "task.interrupted", {"error_code": "BRIDGE_SHUTDOWN"}
                )
        for request_id, waiter in list(self._pending_waiters.items()):
            self._pending_waiters.pop(request_id, None)
            if not waiter.done():
                waiter.cancel()
        if self.adapters:
            await asyncio.gather(
                *(adapter.close() for adapter in self.adapters.values()),
                return_exceptions=True,
            )
        if self.preflight is not None:
            await self.preflight.close()

    async def list_runtimes(self) -> dict[str, Any]:
        runtimes = []
        for name in sorted(self.adapters):
            try:
                info = await self.adapters[name].probe()
            except Exception:
                info = None
            if info is None:
                runtimes.append({"name": name, "available": False})
            else:
                runtimes.append(info.to_dict())
        return {"runtimes": runtimes}

    async def submit_task(
        self,
        *,
        runtime: str,
        workdir: str,
        path: str,
        profile: str,
        prompt: str,
        continue_from_task_id: str | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise BridgeError("BRIDGE_CLOSED", "bridge is closed")
        if not isinstance(runtime, str) or not runtime:
            raise BridgeError("INVALID_REQUEST", "runtime must be a non-empty string")
        if not isinstance(workdir, str) or not workdir:
            raise BridgeError("INVALID_REQUEST", "workdir must be a non-empty string")
        if not isinstance(prompt, str):
            raise BridgeError("INVALID_REQUEST", "prompt must be a string")
        if not isinstance(path, str):
            raise BridgeError("INVALID_REQUEST", "path must be a string")
        if continue_from_task_id is not None and not isinstance(continue_from_task_id, str):
            raise BridgeError("INVALID_REQUEST", "continue_from_task_id must be a string")
        if len(prompt.encode("utf-8")) > self.limits.max_prompt_bytes:
            raise BridgeError("AGENT_PROMPT_TOO_LARGE", "agent prompt exceeds configured limit")
        adapter = self.adapters.get(runtime)
        if adapter is None:
            raise BridgeError("AGENT_RUNTIME_UNAVAILABLE", f"runtime is unavailable: {runtime}")

        try:
            requested_profile = AgentProfile(profile)
        except ValueError as exc:
            raise BridgeError("AGENT_PROFILE_NOT_ALLOWED", "unknown agent profile") from exc
        policy, cwd = self.policies.authorize(
            workdir=workdir,
            runtime=runtime,
            profile=requested_profile,
            relative_cwd=path,
        )

        continue_native_session_id = None
        if continue_from_task_id is not None:
            prior = self.store.get_task(continue_from_task_id)
            if prior.runtime != runtime or prior.workdir_alias != workdir:
                raise BridgeError(
                    "AGENT_SESSION_NOT_RESUMABLE",
                    "continued task must use the same runtime and workdir",
                )
            if (
                prior.profile == AgentProfile.REVIEW.value
                and requested_profile is AgentProfile.WORKSPACE_WRITE
            ):
                raise BridgeError(
                    "AGENT_PROFILE_NOT_ALLOWED",
                    "continuation cannot escalate from review to workspace-write",
                )
            if prior.native_session_id is None:
                raise BridgeError(
                    "AGENT_SESSION_NOT_RESUMABLE",
                    "prior task has no resumable native session",
                )
            continue_native_session_id = prior.native_session_id

        if continue_from_task_id is not None and TaskStatus(prior.status) not in TERMINAL_STATUSES:
            raise BridgeError(
                "AGENT_SESSION_NOT_RESUMABLE",
                "continuation requires a completed prior task",
            )

        preflight_result: dict[str, Any] | None = None
        routing_advice: dict[str, Any] | None = None
        if self.preflight is not None:
            try:
                preflight_result = await self.preflight.evaluate(
                    runtime=runtime,
                    workdir=workdir,
                    path=path,
                    profile=requested_profile.value,
                    prompt=prompt,
                    is_continuation=continue_from_task_id is not None,
                )
            except Exception:
                preflight_result = {"status": "unavailable"}
            routing_advice = _routing_advice_from_preflight(
                preflight_result,
                requested_runtime=runtime,
            )

        async with self._submit_lock:
            lease: WorkdirLease | None = None
            if requested_profile is AgentProfile.WORKSPACE_WRITE:
                lease = self.lease_manager.acquire_exclusive(policy.slot)

            task_id = new_id("agt")
            try:
                task = self.store.create_task(
                    task_id=task_id,
                    runtime=runtime,
                    workdir_alias=workdir,
                    workdir_slot=policy.slot,
                    relative_cwd=path,
                    profile=requested_profile.value,
                    continue_from_task_id=continue_from_task_id,
                    max_active_tasks=self.limits.max_active_tasks,
                )
                if lease is not None:
                    self._leases[task_id] = lease
                if preflight_result is not None:
                    self._try_append_event(task_id, "task.preflight", preflight_result)
                if routing_advice is not None:
                    self._try_append_event(task_id, "task.routing_advice", routing_advice)
                background = asyncio.create_task(
                    self._run_task(
                        task_id=task_id,
                        adapter=adapter,
                        cwd=cwd,
                        prompt=prompt,
                        continue_native_session_id=continue_native_session_id,
                    ),
                    name=f"serverfs-agent-{task_id}",
                )
            except Exception:
                self._leases.pop(task_id, None)
                if lease is not None:
                    lease.release()
                if "task" in locals():
                    self.store.transition_task(
                        task_id,
                        TaskStatus.INTERRUPTED,
                        error_code="BRIDGE_SCHEDULING_FAILED",
                        error_message="bridge could not schedule the task",
                    )
                raise
            self._background[task_id] = background
            background.add_done_callback(
                lambda completed: self._background_done(task_id, completed)
            )
            result: dict[str, Any] = {"task_id": task.task_id, "status": task.status}
            if preflight_result is not None:
                result["preflight"] = preflight_result
            if routing_advice is not None:
                result["routing_advice"] = routing_advice
            return result

    def _background_done(self, task_id: str, task: asyncio.Task[None]) -> None:
        self._background.pop(task_id, None)
        if not task.cancelled():
            task.exception()

    def _release_lease(self, task_id: str) -> None:
        lease = self._leases.pop(task_id, None)
        if lease is not None:
            lease.release()

    def get_task(self, task_id: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id:
            raise BridgeError("INVALID_REQUEST", "task_id must be a non-empty string")
        result = self.store.get_task(task_id).to_dict()
        result.pop("native_session_id", None)
        result.pop("native_turn_id", None)
        pending_id = result.get("pending_request_id")
        if pending_id:
            result["pending_request"] = self.store.get_request(pending_id).to_dict()
        return result

    def read_events(
        self, task_id: str, *, after_event_id: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        if (
            not isinstance(after_event_id, int)
            or isinstance(after_event_id, bool)
            or after_event_id < 0
        ):
            raise BridgeError("INVALID_LIMIT", "event cursor must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 500:
            raise BridgeError("INVALID_LIMIT", "event limit must be between 1 and 500")
        events = self.store.list_events(task_id, after_event_id=after_event_id, limit=limit)
        next_cursor = events[-1].event_id if events else after_event_id
        return {
            "events": [event.to_dict() for event in events],
            "next_after_event_id": next_cursor,
        }

    async def respond_approval(
        self,
        *,
        task_id: str,
        request_id: str,
        decision: str,
        granted_permission_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        _require_identifier(task_id, "task_id")
        _require_identifier(request_id, "request_id")
        request = self.store.get_request(request_id)
        if request.status != RequestStatus.PENDING.value:
            code = (
                "REQUEST_ALREADY_RESOLVED"
                if request.status == RequestStatus.RESOLVED.value
                else "REQUEST_STALE"
            )
            raise BridgeError(code, "approval request is no longer pending")
        task = self.store.get_task(task_id)
        if task.pending_request_id != request_id:
            raise BridgeError("REQUEST_STALE", "approval request is no longer active")
        if request.kind != RequestKind.APPROVAL.value:
            raise BridgeError("INVALID_APPROVAL_DECISION", "request is not an approval")

        try:
            normalized = ApprovalDecision(decision)
        except ValueError as exc:
            raise BridgeError("INVALID_APPROVAL_DECISION", "unsupported approval decision") from exc

        available = set(request.payload.get("available_decisions", []))
        if available and normalized.value not in available:
            raise BridgeError(
                "INVALID_APPROVAL_DECISION",
                "approval decision was not offered by the runtime",
            )

        if granted_permission_ids is not None and (
            not isinstance(granted_permission_ids, list)
            or any(not isinstance(item, str) for item in granted_permission_ids)
        ):
            raise BridgeError("INVALID_APPROVAL_DECISION", "granted_permission_ids must be strings")
        granted = list(granted_permission_ids or [])
        requested_ids = {
            item["permission_id"]
            for item in request.payload.get("requested_permissions", [])
            if isinstance(item, dict) and isinstance(item.get("permission_id"), str)
        }
        if any(item not in requested_ids for item in granted):
            raise BridgeError(
                "PERMISSION_OUTSIDE_POLICY",
                "granted permission was not part of the pending request",
            )

        resolution = {"decision": normalized.value, "granted_permission_ids": granted}
        self._ensure_interaction_size(resolution)
        if normalized is ApprovalDecision.CANCEL_TASK:
            self._user_cancelled.add(task_id)
        self.store.resolve_request(request_id, resolution)
        waiter = self._pending_waiters.pop(request_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(resolution)
        self._try_append_event(task_id, "approval.resolved", {"decision": normalized.value})
        if normalized is ApprovalDecision.CANCEL_TASK:
            adapter = self.adapters.get(task.runtime)
            if adapter is not None:
                try:
                    await adapter.cancel(task_id)
                except Exception:
                    pass
        return {"task_id": task_id, "request_id": request_id, "resolved": True}

    async def answer_question(
        self, *, task_id: str, request_id: str, answers: list[dict[str, Any]]
    ) -> dict[str, Any]:
        _require_identifier(task_id, "task_id")
        _require_identifier(request_id, "request_id")
        request = self.store.get_request(request_id)
        if request.status != RequestStatus.PENDING.value:
            code = (
                "REQUEST_ALREADY_RESOLVED"
                if request.status == RequestStatus.RESOLVED.value
                else "REQUEST_STALE"
            )
            raise BridgeError(code, "question request is no longer pending")
        task = self.store.get_task(task_id)
        if task.pending_request_id != request_id:
            raise BridgeError("REQUEST_STALE", "question request is no longer active")
        if request.kind != RequestKind.QUESTION.value:
            raise BridgeError("INVALID_QUESTION_ANSWER", "request is not a question")

        if not isinstance(answers, list):
            raise BridgeError("INVALID_QUESTION_ANSWER", "answers must be an array")
        normalized = _validate_answers(request.payload, answers)
        resolution = {"answers": normalized}
        self._ensure_interaction_size(resolution)
        self.store.resolve_request(request_id, resolution)
        waiter = self._pending_waiters.pop(request_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(resolution)
        self._try_append_event(task_id, "question.answered", {"question_count": len(normalized)})
        return {"task_id": task_id, "request_id": request_id, "resolved": True}

    async def send_message(self, *, task_id: str, message: str) -> dict[str, Any]:
        _require_identifier(task_id, "task_id")
        if not isinstance(message, str):
            raise BridgeError("INVALID_REQUEST", "message must be a string")
        if len(message.encode("utf-8")) > self.limits.max_message_bytes:
            raise BridgeError("AGENT_MESSAGE_TOO_LARGE", "agent message exceeds configured limit")
        task = self.store.get_task(task_id)
        if TaskStatus(task.status) in TERMINAL_STATUSES:
            raise BridgeError(
                "AGENT_TASK_TERMINAL",
                "task is terminal; submit a continuation instead",
            )
        if TaskStatus(task.status) is TaskStatus.QUEUED:
            raise BridgeError("AGENT_TASK_NOT_ACTIVE", "task has not started yet")
        adapter = self.adapters.get(task.runtime)
        if adapter is None:
            raise BridgeError("AGENT_RUNTIME_UNAVAILABLE", "runtime is unavailable")
        await adapter.send_message(task_id, message)
        self._try_append_event(
            task_id,
            "user.message",
            {"bytes": len(message.encode("utf-8"))},
        )
        return {"task_id": task_id, "accepted": True}

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        _require_identifier(task_id, "task_id")
        async with self._cancel_lock:
            task = self.store.get_task(task_id)
            status = TaskStatus(task.status)
            if status in TERMINAL_STATUSES:
                return {"task_id": task_id, "status": task.status}

            first_cancel = task_id not in self._user_cancelled
            self._user_cancelled.add(task_id)
            adapter = self.adapters.get(task.runtime)
            if first_cancel and adapter is not None:
                try:
                    await adapter.cancel(task_id)
                except Exception:
                    # The task is still cancelled locally.  The provider may be
                    # unavailable, but it must not turn a user cancellation into
                    # an unhandled RPC failure or leave the lease held forever.
                    pass

            background = self._background.get(task_id)
            if background is not None and not background.done():
                background.cancel()
                current = self.store.get_task(task_id)
                if TaskStatus(current.status) is TaskStatus.QUEUED:
                    self.store.transition_task(task_id, TaskStatus.CANCELLED)
                    self._release_lease(task_id)
                    self._try_append_event(task_id, "task.cancelled", {})
            else:
                self.store.transition_task(task_id, TaskStatus.CANCELLED)
                self._release_lease(task_id)
                self._try_append_event(task_id, "task.cancelled", {})
            return {"task_id": task_id, "status": TaskStatus.CANCELLED.value}

    async def _run_task(
        self,
        *,
        task_id: str,
        adapter: AgentAdapter,
        cwd: Path,
        prompt: str,
        continue_native_session_id: str | None,
    ) -> None:
        task = self.store.get_task(task_id)
        try:
            self.store.transition_task(task_id, TaskStatus.STARTING)
            self._append_event(task_id, "task.started", {"runtime": task.runtime})

            self.store.transition_task(task_id, TaskStatus.RUNNING)

            context = TaskContext(
                task_id=task_id,
                workdir=task.workdir_alias,
                workdir_root=self.policies.get(task.workdir_alias).host_path,
                cwd=cwd,
                profile=task.profile,
                prompt=prompt,
                continue_native_session_id=continue_native_session_id,
                emit_event=lambda event_type, payload: self._emit_event(
                    task_id, event_type, payload
                ),
                request_approval=lambda payload: self._wait_for_request(
                    task_id,
                    kind=RequestKind.APPROVAL,
                    payload=payload,
                    waiting_status=TaskStatus.WAITING_FOR_APPROVAL,
                ),
                ask_question=lambda payload: self._wait_for_request(
                    task_id,
                    kind=RequestKind.QUESTION,
                    payload=payload,
                    waiting_status=TaskStatus.WAITING_FOR_QUESTION,
                ),
                abandon_interaction=lambda: self._abandon_interaction(task_id),
            )
            if continue_native_session_id is None:
                result = await adapter.run_task(context)
            else:
                result = await adapter.continue_task(context)
            self.store.set_native_ids(
                task_id,
                native_session_id=result.native_session_id,
                native_turn_id=result.native_turn_id,
            )
            if not isinstance(result.final_response, str):
                raise BridgeError(
                    "AGENT_PROVIDER_ERROR", "agent runtime returned an invalid result"
                )
            final_response = self._redact_text(task.workdir_alias, result.final_response)
            encoded = final_response.encode("utf-8")
            if len(encoded) > self.limits.max_final_response_bytes:
                raise BridgeError(
                    "AGENT_RESULT_TOO_LARGE",
                    "final agent response exceeds configured limit",
                )
            if task_id in self._user_cancelled:
                self.store.transition_task(task_id, TaskStatus.CANCELLED)
                self._try_append_event(task_id, "task.cancelled", {})
            elif self._closed:
                self.store.transition_task(
                    task_id,
                    TaskStatus.INTERRUPTED,
                    error_code="BRIDGE_SHUTDOWN",
                    error_message="bridge stopped while task was active",
                )
                self._try_append_event(
                    task_id, "task.interrupted", {"error_code": "BRIDGE_SHUTDOWN"}
                )
            else:
                self.store.transition_task(
                    task_id,
                    TaskStatus.SUCCEEDED,
                    final_response=final_response,
                )
                self._try_append_event(task_id, "task.completed", {})
        except asyncio.CancelledError:
            current = self.store.get_task(task_id)
            if TaskStatus(current.status) not in TERMINAL_STATUSES:
                if task_id in self._user_cancelled:
                    self.store.transition_task(task_id, TaskStatus.CANCELLED)
                    self._try_append_event(task_id, "task.cancelled", {})
                else:
                    self.store.transition_task(
                        task_id,
                        TaskStatus.INTERRUPTED,
                        error_code="BRIDGE_SHUTDOWN",
                        error_message="bridge stopped while task was active",
                    )
                    self._try_append_event(
                        task_id,
                        "task.interrupted",
                        {"error_code": "BRIDGE_SHUTDOWN"},
                    )
        except BridgeError as exc:
            current = self.store.get_task(task_id)
            if TaskStatus(current.status) in (
                TaskStatus.WAITING_FOR_APPROVAL,
                TaskStatus.WAITING_FOR_QUESTION,
            ):
                self.store.transition_task(
                    task_id,
                    TaskStatus.INTERRUPTED,
                    error_code=exc.code,
                    error_message=self._redact_text(task.workdir_alias, exc.message),
                )
                self._try_append_event(task_id, "task.interrupted", {"error_code": exc.code})
            elif TaskStatus(current.status) not in TERMINAL_STATUSES:
                self.store.transition_task(
                    task_id,
                    TaskStatus.FAILED,
                    error_code=exc.code,
                    error_message=self._redact_text(task.workdir_alias, exc.message),
                )
                self._try_append_event(task_id, "task.failed", {"error_code": exc.code})
        except Exception:
            current = self.store.get_task(task_id)
            if TaskStatus(current.status) in (
                TaskStatus.WAITING_FOR_APPROVAL,
                TaskStatus.WAITING_FOR_QUESTION,
            ):
                self.store.transition_task(
                    task_id,
                    TaskStatus.INTERRUPTED,
                    error_code="AGENT_PROVIDER_ERROR",
                    error_message="agent runtime failed while waiting for input",
                )
                self._try_append_event(
                    task_id, "task.interrupted", {"error_code": "AGENT_PROVIDER_ERROR"}
                )
            elif TaskStatus(current.status) not in TERMINAL_STATUSES:
                self.store.transition_task(
                    task_id,
                    TaskStatus.FAILED,
                    error_code="AGENT_PROVIDER_ERROR",
                    error_message="agent runtime failed",
                )
                self._try_append_event(
                    task_id, "task.failed", {"error_code": "AGENT_PROVIDER_ERROR"}
                )
        finally:
            self._user_cancelled.discard(task_id)
            self._release_lease(task_id)
            for request_id, waiter in list(self._pending_waiters.items()):
                if not waiter.done():
                    try:
                        req = self.store.get_request(request_id)
                    except BridgeError:
                        continue
                    if req.task_id == task_id:
                        waiter.cancel()
                        self._pending_waiters.pop(request_id, None)

    async def _emit_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._append_event(task_id, event_type, payload)

    def _try_append_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self._append_event(task_id, event_type, payload)
        except BridgeError:
            pass

    def _append_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        redacted_payload = self._redact_value(task_id, payload)
        encoded = json.dumps(redacted_payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > self.limits.max_event_bytes:
            raise BridgeError("AGENT_EVENT_TOO_LARGE", "agent event exceeds configured limit")
        self.store.append_event(
            task_id,
            event_type,
            redacted_payload,
            max_events_per_task=self.limits.max_events_per_task,
        )

    def _ensure_interaction_size(self, payload: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise BridgeError(
                "AGENT_PROVIDER_ERROR", "approval or question payload is not JSON"
            ) from exc
        if len(encoded) > self.limits.max_interaction_bytes:
            raise BridgeError(
                "AGENT_INTERACTION_TOO_LARGE",
                "approval or question payload exceeds configured limit",
            )

    def _redact_text(self, workdir_alias: str, value: str) -> str:
        root = self.policies.get(workdir_alias).host_path
        if Path(value).is_absolute():
            relative = redact_host_path(root, value)
            # A bare workdir-relative path reads as an unrelated token in agent
            # prose, so this channel keeps the `<workdir>/...` display form that
            # _redact_embedded_workdir also produces.
            if relative == "<outside-workdir>" or relative == ".":
                return relative
            return f"<workdir>/{relative}"
        return _redact_embedded_workdir(root, value)

    def _redact_value(self, task_id: str, value: Any) -> Any:
        task = self.store.get_task(task_id)
        root = self.policies.get(task.workdir_alias).host_path
        return self._redact_value_for_root(root, value)

    def _redact_value_for_root(self, root: Path, value: Any) -> Any:
        private_keys = {"native_session_id", "native_turn_id", "thread_id", "turn_id"}
        if isinstance(value, str):
            if Path(value).is_absolute():
                return redact_host_path(root, value)
            return _redact_embedded_workdir(root, value)
        if isinstance(value, Path):
            return redact_host_path(root, value)
        if isinstance(value, dict):
            path_keys = {
                "path",
                "cwd",
                "relative_cwd",
                "host_path",
                "file_path",
                "grantRoot",
            }
            path_list_keys = {"read", "write", "readableRoots", "writableRoots"}
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                if key in private_keys:
                    continue
                if key in path_keys and isinstance(item, (str, Path)):
                    redacted[key] = redact_host_path(root, item)
                    continue
                if key in path_list_keys and isinstance(item, list):
                    redacted[key] = [
                        redact_host_path(root, child)
                        if isinstance(child, (str, Path))
                        else self._redact_value_for_root(root, child)
                        for child in item
                    ]
                    continue
                redacted[key] = self._redact_value_for_root(root, item)
            return redacted
        if isinstance(value, list):
            return [self._redact_value_for_root(root, item) for item in value]
        if isinstance(value, tuple):
            return [self._redact_value_for_root(root, item) for item in value]
        return value

    async def _abandon_interaction(self, task_id: str) -> None:
        request_id = self.store.abandon_pending_request(task_id)
        if request_id is None:
            return
        waiter = self._pending_waiters.pop(request_id, None)
        if waiter is not None and not waiter.done():
            waiter.cancel()
        self._try_append_event(
            task_id,
            "interaction.stale",
            {"request_id": request_id},
        )

    async def _wait_for_request(
        self,
        task_id: str,
        *,
        kind: RequestKind,
        payload: dict[str, Any],
        waiting_status: TaskStatus,
    ) -> dict[str, Any]:
        request_id = new_id("req")
        normalized_payload = self._redact_value(task_id, payload)
        if not isinstance(normalized_payload, dict):
            raise BridgeError("AGENT_PROVIDER_ERROR", "provider request payload is invalid")
        self._ensure_interaction_size(normalized_payload)
        self.store.create_pending_request(
            task_id=task_id,
            request_id=request_id,
            kind=kind.value,
            payload=normalized_payload,
            waiting_status=waiting_status,
        )
        self._append_event(
            task_id,
            f"{kind.value}.requested",
            {"request_id": request_id},
        )
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_waiters[request_id] = waiter
        request = self.store.get_request(request_id)
        if request.status == "resolved":
            waiter.set_result(request.resolution or {})
        elif request.status == "stale":
            waiter.cancel()
        try:
            return await waiter
        finally:
            self._pending_waiters.pop(request_id, None)


def _routing_advice_from_preflight(
    preflight: dict[str, Any],
    *,
    requested_runtime: str,
) -> dict[str, Any]:
    status = preflight.get("status")
    if status != "completed":
        return {"status": status or "unavailable"}

    answers = preflight.get("answers")
    if not isinstance(answers, dict):
        return {"status": "unavailable"}
    recommendation = answers.get("route_recommendation")
    if not isinstance(recommendation, dict):
        return {"status": "unavailable"}

    choice = recommendation.get("choice")
    if not isinstance(choice, str):
        return {"status": "unavailable"}

    return {
        "status": "completed",
        "model": preflight.get("model"),
        "requested_runtime": requested_runtime,
        "recommendation": recommendation,
        "matches_requested_runtime": choice == requested_runtime,
        "automatic": False,
    }


def _redact_embedded_workdir(root: Path, value: str) -> str:
    root_text = str(root)
    start = 0
    chunks: list[str] = []
    while True:
        index = value.find(root_text, start)
        if index < 0:
            chunks.append(value[start:])
            return "".join(chunks)

        before = value[index - 1] if index > 0 else ""
        end = index + len(root_text)
        after = value[end] if end < len(value) else ""
        before_ok = not before or before.isspace() or before in "'\"([{<,:="
        after_ok = not after or after == "/" or after.isspace() or after in "'\")]}>,:;"

        if before_ok and after_ok:
            chunks.append(value[start:index])
            chunks.append("<workdir>")
            start = end
            continue

        if before_ok and after and not after.isspace():
            # The token starts like the configured workdir but isn't that
            # directory or one of its descendants (for example repo2 next to
            # repo). Treat the whole absolute path token as outside rather
            # than leaking the host root prefix in free text.
            token_end = _embedded_path_token_end(value, end)
            chunks.append(value[start:index])
            chunks.append("<outside-workdir>")
            start = token_end
            continue

        chunks.append(value[start:end])
        start = end


def _embedded_path_token_end(value: str, start: int) -> int:
    terminators = set(" \t\r\n'\"()[]{}<>,;")
    end = start
    while end < len(value) and value[end] not in terminators:
        end += 1
    # Sentence punctuation abutting a path is ordinary prose, not part of the
    # token, so keep it outside the redaction marker.
    while end > start and value[end - 1] in ".!?:":
        end -= 1
    return end


def _validate_answers(
    payload: dict[str, Any], answers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    questions = payload.get("questions")
    if not isinstance(questions, list):
        raise BridgeError("INVALID_QUESTION_ANSWER", "pending question payload is invalid")

    by_id: dict[str, dict[str, Any]] = {}
    for question in questions:
        if isinstance(question, dict) and isinstance(question.get("question_id"), str):
            by_id[question["question_id"]] = question

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for answer in answers:
        if not isinstance(answer, dict):
            raise BridgeError("INVALID_QUESTION_ANSWER", "answers must be objects")
        if set(answer) - {"question_id", "selected_option_ids", "text"}:
            raise BridgeError("INVALID_QUESTION_ANSWER", "answer fields are not supported")
        question_id = answer.get("question_id")
        if not isinstance(question_id, str) or question_id not in by_id or question_id in seen:
            raise BridgeError("INVALID_QUESTION_ANSWER", "unknown or duplicate question_id")
        seen.add(question_id)
        question = by_id[question_id]

        selected = answer.get("selected_option_ids", [])
        text = answer.get("text")
        if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
            raise BridgeError("INVALID_QUESTION_ANSWER", "selected_option_ids must be strings")
        if len(selected) != len(set(selected)):
            raise BridgeError("INVALID_QUESTION_ANSWER", "answer selected a duplicate option")
        option_ids = {
            option["option_id"]
            for option in question.get("options", [])
            if isinstance(option, dict) and isinstance(option.get("option_id"), str)
        }
        if any(item not in option_ids for item in selected):
            raise BridgeError("INVALID_QUESTION_ANSWER", "answer selected an unknown option")
        if not question.get("multi_select", False) and len(selected) > 1:
            raise BridgeError("INVALID_QUESTION_ANSWER", "question allows only one option")
        if text is not None and not isinstance(text, str):
            raise BridgeError("INVALID_QUESTION_ANSWER", "free-text answer must be a string")
        if text is not None and not question.get("allow_free_text", False):
            raise BridgeError("INVALID_QUESTION_ANSWER", "question does not allow free text")
        if not selected and not text:
            raise BridgeError(
                "INVALID_QUESTION_ANSWER", "answer must select an option or provide text"
            )
        normalized.append(
            {
                "question_id": question_id,
                "selected_option_ids": selected,
                **({"text": text} if text is not None else {}),
            }
        )

    if set(by_id) != seen:
        raise BridgeError("INVALID_QUESTION_ANSWER", "every question requires an answer")
    return normalized


def _require_identifier(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise BridgeError("INVALID_REQUEST", f"{name} must be a non-empty string")

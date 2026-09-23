"""Codex App Server adapter for the ServerFS Agent Bridge.

The adapter reuses the official managed Codex App Server daemon.  It owns no
Codex worker lifecycle beyond an optional, administrator-enabled invocation of
the official idempotent `codex app-server daemon start` command.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from ..config import CodexSettings
from ..errors import BridgeError
from ..models import AgentProfile, ReconciliationStatus, RuntimeInfo, TaskRecord, TaskStatus
from .base import AdapterResult, AgentAdapter, ReconcileResult, TaskContext
from .codex_transport import CONTROL_SOCKET_UNAVAILABLE_MESSAGE, CodexConnection

_COMMAND_APPROVAL = "item/commandExecution/requestApproval"
_FILE_APPROVAL = "item/fileChange/requestApproval"
_PERMISSION_APPROVAL = "item/permissions/requestApproval"
_USER_INPUT = "item/tool/requestUserInput"
_SERVER_REQUEST_RESOLVED = "serverRequest/resolved"
_TURN_COMPLETED = "turn/completed"
_TRANSPORT_CLOSED = "_serverfs/transportClosed"

_NATIVE_DECISIONS = {
    "approve_once": "accept",
    "approve_session": "acceptForSession",
    "deny": "decline",
    "cancel_task": "cancel",
}


@dataclass
class _ActiveTask:
    context: TaskContext
    connection: CodexConnection
    thread_id: str = ""
    turn_id: str = ""
    turn_ready: asyncio.Event = field(default_factory=asyncio.Event)
    final_messages: list[tuple[str | None, str]] = field(default_factory=list)
    request_handlers: dict[str, asyncio.Task[None]] = field(default_factory=dict)


class CodexAdapter(AgentAdapter):
    def __init__(
        self,
        settings: CodexSettings,
        *,
        client_version: str = "0.7.0",
    ) -> None:
        self.settings = settings
        self.client_version = client_version
        self._active: dict[str, _ActiveTask] = {}
        self._active_lock = asyncio.Lock()
        self._cancel_requested: set[str] = set()
        self._closed = False

    @property
    def name(self) -> str:
        return "codex"

    async def probe(self) -> RuntimeInfo:
        if not self.settings.enabled or self._closed:
            return self._runtime_info(available=False)
        connection = self._new_connection()
        try:
            await connection.connect()
        except BridgeError:
            return self._runtime_info(available=False)
        try:
            return self._runtime_info(
                available=True,
                version=connection.server_version,
            )
        finally:
            await connection.close()

    async def run_task(self, context: TaskContext) -> AdapterResult:
        return await self._run(context, resume=False)

    async def continue_task(self, context: TaskContext) -> AdapterResult:
        if not context.continue_native_session_id:
            raise BridgeError(
                "AGENT_SESSION_NOT_RESUMABLE",
                "Codex continuation requires a native thread id",
            )
        return await self._run(context, resume=True)

    async def get_state(self, task_id: str) -> str | None:
        active = self._active.get(task_id)
        return "running" if active is not None else None

    async def send_message(self, task_id: str, message: str) -> None:
        active = self._active.get(task_id)
        if active is None:
            raise BridgeError("AGENT_TASK_NOT_ACTIVE", "Codex task is not active")
        await active.turn_ready.wait()
        if not active.turn_id:
            raise BridgeError("AGENT_TASK_NOT_ACTIVE", "Codex turn is no longer steerable")
        await active.connection.request(
            "turn/steer",
            {
                "threadId": active.thread_id,
                "input": [{"type": "text", "text": message}],
                "expectedTurnId": active.turn_id,
            },
        )

    async def cancel(self, task_id: str) -> None:
        active = self._active.get(task_id)
        if active is None:
            return
        self._cancel_requested.add(task_id)
        # A cancellation may race the thread/turn handshake.  The task already
        # reports ``running``, so wait for the native turn id instead of
        # interrupting with an empty turn id, which Codex would reject.
        await active.turn_ready.wait()
        if not active.turn_id:
            return
        try:
            await active.connection.request(
                "turn/interrupt",
                {"threadId": active.thread_id, "turnId": active.turn_id},
            )
        except BridgeError:
            # Local Bridge cancellation still proceeds.  A lost provider
            # connection must not turn a user cancellation into an RPC failure.
            return

    async def reconcile(self) -> None:
        return None

    async def reconcile_task(self, task: TaskRecord) -> ReconcileResult:
        if not task.native_session_id:
            if (
                task.status == TaskStatus.FAILED.value
                and task.error_code == "AGENT_RUNTIME_NOT_READY"
                and task.error_message == CONTROL_SOCKET_UNAVAILABLE_MESSAGE
                and not task.native_turn_id
            ):
                return ReconcileResult(
                    status=ReconciliationStatus.NOT_RECOVERABLE,
                    provider_active=False,
                    detail="Codex control socket failed before a native thread or turn started",
                )
            return ReconcileResult(
                status=ReconciliationStatus.NOT_RECOVERABLE,
                provider_active=None,
                detail="task has no persisted Codex thread id",
            )

        connection = self._new_connection()
        try:
            await connection.connect()
            result = await connection.request(
                "thread/read",
                {"threadId": task.native_session_id, "includeTurns": True},
            )
        except BridgeError:
            return ReconcileResult(
                status=ReconciliationStatus.UNKNOWN,
                provider_active=None,
                detail="Codex thread state could not be verified",
            )
        finally:
            try:
                await connection.close()
            except Exception:
                pass

        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != task.native_session_id:
            return ReconcileResult(
                status=ReconciliationStatus.UNKNOWN,
                provider_active=None,
                detail="Codex returned a different or invalid thread",
            )

        thread_status = thread.get("status")
        turns = thread.get("turns")
        target_turn = None
        if isinstance(turns, list) and task.native_turn_id:
            for turn in turns:
                if isinstance(turn, dict) and turn.get("id") == task.native_turn_id:
                    target_turn = turn
                    break

        if thread_status == "active":
            if isinstance(target_turn, dict) and target_turn.get("status") == "inProgress":
                return ReconcileResult(
                    status=ReconciliationStatus.UNKNOWN,
                    provider_active=True,
                    detail=(
                        "exact Codex turn is still active; Bridge cannot safely resume "
                        "event consumption"
                    ),
                )
            return ReconcileResult(
                status=ReconciliationStatus.UNKNOWN,
                provider_active=True,
                detail="Codex thread is active but exact turn ownership is not recoverable",
            )

        return ReconcileResult(
            status=ReconciliationStatus.SESSION_RESUMABLE,
            provider_active=False,
            detail="Codex thread is no longer active and remains resumable",
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        active = list(self._active.values())
        self._active.clear()
        for item in active:
            await self._cancel_request_handlers(item)
        if active:
            await asyncio.gather(
                *(item.connection.close() for item in active),
                return_exceptions=True,
            )

    async def _run(self, context: TaskContext, *, resume: bool) -> AdapterResult:
        if self._closed:
            raise BridgeError("AGENT_RUNTIME_UNAVAILABLE", "Codex adapter is closed")
        if not self.settings.enabled:
            raise BridgeError("AGENT_RUNTIME_UNAVAILABLE", "Codex runtime is disabled")
        if context.profile != AgentProfile.WORKSPACE_WRITE.value:
            raise BridgeError(
                "AGENT_PROFILE_NOT_ALLOWED",
                "Codex native mode requires workspace-write so the Bridge holds the workdir lease",
            )

        # ServerFS already reports the task as ``running`` when this callback is
        # invoked, so register before the first await and gate steer/cancel on
        # ``turn_ready``.  Otherwise a caller that reads ``running`` and steers
        # immediately hits a spurious "not active" error that Phase A's adapter
        # never produced.
        active = _ActiveTask(context=context, connection=self._new_connection())
        async with self._active_lock:
            if context.task_id in self._active:
                raise BridgeError("AGENT_PROVIDER_ERROR", "Codex task is already active")
            self._active[context.task_id] = active

        connection: CodexConnection | None = None
        try:
            connection = await self._connect_with_optional_start()
            active.connection = connection

            if resume:
                active.thread_id = await self._resume_thread(connection, context)
            else:
                active.thread_id = await self._start_thread(connection, context)
            await context.record_native_ids(active.thread_id, None)
            active.turn_id = await self._start_turn(connection, context, active.thread_id)
            await context.record_native_ids(active.thread_id, active.turn_id)
            active.turn_ready.set()

            return await self._consume_turn(active)
        finally:
            # Release steer/cancel waiters even when the handshake failed.
            active.turn_ready.set()
            async with self._active_lock:
                self._active.pop(context.task_id, None)
            await self._cancel_request_handlers(active)
            self._cancel_requested.discard(context.task_id)
            if connection is not None:
                await connection.close()

    async def _start_thread(
        self,
        connection: CodexConnection,
        context: TaskContext,
    ) -> str:
        result = await connection.request(
            "thread/start",
            {
                "cwd": str(context.cwd),
                "serviceName": "serverfs-agent-bridge",
            },
        )
        return _thread_id_from_result(result)

    async def _resume_thread(
        self,
        connection: CodexConnection,
        context: TaskContext,
    ) -> str:
        result = await connection.request(
            "thread/resume",
            {
                "threadId": context.continue_native_session_id,
                "cwd": str(context.cwd),
            },
        )
        resumed_id = _thread_id_from_result(result)
        if resumed_id != context.continue_native_session_id:
            raise BridgeError(
                "AGENT_SESSION_NOT_RESUMABLE",
                "Codex resumed a different thread",
            )
        return resumed_id

    async def _start_turn(
        self,
        connection: CodexConnection,
        context: TaskContext,
        thread_id: str,
    ) -> str:
        result = await connection.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": context.prompt}],
                "cwd": str(context.cwd),
            },
        )
        return _turn_id_from_result(result)

    def _event_timeout(self, active: _ActiveTask) -> float | None:
        timeout = self.settings.event_idle_timeout_seconds
        if timeout is None:
            return None
        if any(not task.done() for task in active.request_handlers.values()):
            # Codex is legitimately silent while a human interaction is
            # outstanding, so the idle timeout must not fail a healthy turn that
            # is simply waiting for the user to answer.
            return None
        return timeout

    async def _consume_turn(self, active: _ActiveTask) -> AdapterResult:
        while True:
            event = await active.connection.next_event(timeout=self._event_timeout(active))
            method = event.get("method")
            params = event.get("params")
            if not isinstance(method, str):
                continue
            if not isinstance(params, dict):
                params = {}

            if method == _TRANSPORT_CLOSED:
                raise BridgeError(
                    "AGENT_PROVIDER_DISCONNECTED",
                    "Codex App Server connection was lost",
                )

            if method != _TRANSPORT_CLOSED and not _belongs_to_active(
                params,
                thread_id=active.thread_id,
                turn_id=active.turn_id,
            ):
                continue

            if "id" in event:
                self._start_server_request(active, event)
                continue

            if method == _SERVER_REQUEST_RESOLVED:
                await self._handle_request_resolved(active, params)
                continue

            if method == "item/started":
                await self._emit_item_event(active, "item.started", params)
                continue
            if method == "item/completed":
                self._capture_final_message(active, params)
                await self._emit_item_event(active, "item.completed", params)
                continue
            if method == "error":
                await active.context.emit_event(
                    "provider.error",
                    {
                        "message": _provider_error_message(params),
                        "will_retry": bool(params.get("willRetry", False)),
                    },
                )
                continue
            if method == "turn/started":
                await active.context.emit_event("turn.started", {})
                continue
            if method == _TURN_COMPLETED:
                return await self._finish_turn(active, params)

    def _start_server_request(
        self,
        active: _ActiveTask,
        message: dict[str, Any],
    ) -> None:
        request_id = str(message.get("id"))
        if request_id in active.request_handlers:
            return
        handler = asyncio.create_task(
            self._handle_server_request(active, message),
            name=f"serverfs-codex-request-{request_id}",
        )
        active.request_handlers[request_id] = handler

        def done(completed: asyncio.Task[None]) -> None:
            active.request_handlers.pop(request_id, None)
            if not completed.cancelled():
                completed.exception()

        handler.add_done_callback(done)

    async def _handle_server_request(
        self,
        active: _ActiveTask,
        message: dict[str, Any],
    ) -> None:
        method = message.get("method")
        params = message.get("params")
        request_id = message.get("id")
        if not isinstance(method, str) or not isinstance(params, dict):
            await active.connection.respond_error(request_id)
            return
        try:
            if method == _COMMAND_APPROVAL:
                result = await self._command_approval(active, params)
            elif method == _FILE_APPROVAL:
                result = await self._file_approval(active, params)
            elif method == _PERMISSION_APPROVAL:
                result = await self._permission_approval(active, params)
            elif method == _USER_INPUT:
                result = await self._user_input(active, params)
            else:
                await active.connection.respond_error(request_id)
                return
            await active.connection.respond(request_id, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A provider request must always receive a response when still live.
            # Provider schema drift or an unexpected adapter bug must not leave
            # Codex waiting forever for a JSON-RPC response.
            try:
                await active.connection.respond_error(
                    request_id,
                    code=-32000,
                    message="ServerFS could not satisfy the interaction request",
                )
            except BridgeError:
                pass

    async def _command_approval(
        self,
        active: _ActiveTask,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "category": "command",
            "title": "Codex command approval",
            "available_decisions": _available_decisions(params.get("availableDecisions")),
        }
        reason = params.get("reason")
        if isinstance(reason, str):
            payload["reason"] = reason
        command = params.get("command")
        if isinstance(command, str):
            payload["command_display"] = command
        elif isinstance(command, list) and all(isinstance(item, str) for item in command):
            payload["command_display"] = " ".join(command)
        cwd = params.get("cwd")
        if isinstance(cwd, str):
            payload["relative_cwd"] = cwd
        additional = params.get("additionalPermissions")
        if isinstance(additional, dict):
            payload["additional_permissions"] = additional
        network = params.get("networkApprovalContext")
        if isinstance(network, dict):
            payload["network_approval_context"] = network

        resolution = await active.context.request_approval(payload)
        return {"decision": _native_decision(resolution)}

    async def _file_approval(
        self,
        active: _ActiveTask,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "category": "file_change",
            "title": "Codex file-change approval",
            "available_decisions": _available_decisions(params.get("availableDecisions")),
        }
        reason = params.get("reason")
        if isinstance(reason, str):
            payload["reason"] = reason
        grant_root = params.get("grantRoot")
        if isinstance(grant_root, str):
            payload["file_changes"] = [{"path": grant_root}]

        resolution = await active.context.request_approval(payload)
        return {"decision": _native_decision(resolution)}

    async def _permission_approval(
        self,
        active: _ActiveTask,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        permissions = params.get("permissions")
        if not isinstance(permissions, dict) or not permissions:
            return {"permissions": {}, "scope": "turn"}

        requested = [
            {
                "permission_id": key,
                "kind": key,
                "details": value,
            }
            for key, value in permissions.items()
            if isinstance(key, str)
        ]
        if not requested:
            return {"permissions": {}, "scope": "turn"}

        payload = {
            "category": "provider_permission",
            "title": "Codex permission request",
            "reason": params.get("reason") if isinstance(params.get("reason"), str) else None,
            "requested_permissions": requested,
            "available_decisions": [
                "approve_once",
                "approve_session",
                "deny",
                "cancel_task",
            ],
        }
        resolution = await active.context.request_approval(payload)
        decision = resolution.get("decision")
        if decision not in ("approve_once", "approve_session"):
            return {"permissions": {}, "scope": "turn"}

        granted_ids = resolution.get("granted_permission_ids")
        if isinstance(granted_ids, list) and granted_ids:
            granted = {key: value for key, value in permissions.items() if key in granted_ids}
        else:
            granted = dict(permissions)
        return {
            "permissions": granted,
            "scope": "session" if decision == "approve_session" else "turn",
        }

    async def _user_input(
        self,
        active: _ActiveTask,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        questions = params.get("questions")
        if not isinstance(questions, list):
            raise BridgeError("AGENT_PROVIDER_ERROR", "Codex user-input request is invalid")

        normalized: list[dict[str, Any]] = []
        option_labels: dict[str, dict[str, str]] = {}
        for index, question in enumerate(questions):
            if not isinstance(question, dict):
                raise BridgeError("AGENT_PROVIDER_ERROR", "Codex user-input question is invalid")
            if bool(question.get("isSecret", False)):
                raise BridgeError(
                    "PERMISSION_OUTSIDE_POLICY",
                    "secret Codex user input is not persisted by ServerFS",
                )
            question_id = question.get("id")
            prompt = question.get("question")
            if not isinstance(question_id, str) or not isinstance(prompt, str):
                raise BridgeError("AGENT_PROVIDER_ERROR", "Codex user-input question is invalid")
            options = question.get("options")
            option_items: list[dict[str, str]] = []
            labels: dict[str, str] = {}
            if isinstance(options, list):
                for option_index, option in enumerate(options):
                    if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                        continue
                    option_id = f"opt_{index}_{option_index}"
                    label = option["label"]
                    labels[option_id] = label
                    option_items.append(
                        {
                            "option_id": option_id,
                            "label": label,
                            "description": option.get("description")
                            if isinstance(option.get("description"), str)
                            else "",
                        }
                    )
            option_labels[question_id] = labels
            normalized.append(
                {
                    "question_id": question_id,
                    "prompt": prompt,
                    "options": option_items,
                    "multi_select": False,
                    "allow_free_text": bool(question.get("isOther", False)) or not option_items,
                }
            )

        resolution = await active.context.ask_question({"questions": normalized})
        answers = resolution.get("answers")
        if not isinstance(answers, list):
            raise BridgeError("AGENT_PROVIDER_ERROR", "ServerFS returned invalid answers")

        native_answers: dict[str, dict[str, list[str]]] = {}
        for answer in answers:
            if not isinstance(answer, dict):
                continue
            question_id = answer.get("question_id")
            if not isinstance(question_id, str) or question_id not in option_labels:
                continue
            values: list[str] = []
            selected = answer.get("selected_option_ids")
            if isinstance(selected, list):
                for option_id in selected:
                    label = option_labels[question_id].get(option_id)
                    if label is not None:
                        values.append(label)
            text = answer.get("text")
            if isinstance(text, str) and text:
                values.append(text)
            native_answers[question_id] = {"answers": values}
        return {"answers": native_answers}

    async def _handle_request_resolved(
        self,
        active: _ActiveTask,
        params: dict[str, Any],
    ) -> None:
        native_id = params.get("requestId")
        if native_id is None:
            return
        request_id = str(native_id)
        handler = active.request_handlers.get(request_id)
        if handler is None or handler.done():
            return
        # Stale the ServerFS request before unwinding the handler, so a ChatGPT
        # answer racing this notification is rejected rather than recorded
        # against a provider request Codex has already cleared.
        await active.context.abandon_interaction()
        handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)

    async def _finish_turn(
        self,
        active: _ActiveTask,
        params: dict[str, Any],
    ) -> AdapterResult:
        await self._cancel_request_handlers(active)
        turn = params.get("turn")
        if not isinstance(turn, dict):
            raise BridgeError("AGENT_PROVIDER_ERROR", "Codex turn completed without turn data")
        status = turn.get("status")
        if status == "interrupted":
            if active.context.task_id in self._cancel_requested:
                raise asyncio.CancelledError
            raise BridgeError(
                "AGENT_PROVIDER_ERROR",
                "Codex turn was interrupted outside a ServerFS cancellation",
            )
        if status != "completed":
            error = turn.get("error")
            message = _provider_error_message(error if isinstance(error, dict) else {})
            raise BridgeError(
                "AGENT_PROVIDER_ERROR",
                message or f"Codex turn ended with status {status!r}",
            )

        final = _choose_final_message(active.final_messages)
        return AdapterResult(
            final_response=final,
            native_session_id=active.thread_id,
            native_turn_id=active.turn_id,
        )

    async def _emit_item_event(
        self,
        active: _ActiveTask,
        event_type: str,
        params: dict[str, Any],
    ) -> None:
        item = params.get("item")
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        payload: dict[str, Any] = {"type": item_type if isinstance(item_type, str) else "unknown"}
        if item_type == "agentMessage":
            text = item.get("text")
            if isinstance(text, str):
                payload["text"] = text
            phase = item.get("phase")
            if isinstance(phase, str):
                payload["phase"] = phase
        elif item_type == "commandExecution":
            payload["command"] = item.get("command")
            payload["cwd"] = item.get("cwd")
            payload["status"] = item.get("status")
        elif item_type == "fileChange":
            payload["status"] = item.get("status")
            payload["changes"] = item.get("changes")
        else:
            status = item.get("status")
            if isinstance(status, str):
                payload["status"] = status
        await active.context.emit_event(event_type, payload)

    def _capture_final_message(
        self,
        active: _ActiveTask,
        params: dict[str, Any],
    ) -> None:
        item = params.get("item")
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            return
        text = item.get("text")
        if not isinstance(text, str):
            return
        phase = item.get("phase") if isinstance(item.get("phase"), str) else None
        active.final_messages.append((phase, text))

    async def _cancel_request_handlers(self, active: _ActiveTask) -> None:
        pending = [task for task in active.request_handlers.values() if not task.done()]
        if not pending:
            active.request_handlers.clear()
            return
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        active.request_handlers.clear()
        try:
            await active.context.abandon_interaction()
        except BridgeError:
            pass

    async def _connect_with_optional_start(self) -> CodexConnection:
        connection = self._new_connection()
        try:
            await connection.connect()
            return connection
        except BridgeError:
            await connection.close()
            if not self.settings.autostart:
                raise
        await self._start_official_daemon()
        connection = self._new_connection()
        await connection.connect()
        return connection

    def _new_connection(self) -> CodexConnection:
        return CodexConnection(
            socket_path=self.settings.control_socket,
            client_name="serverfs-agent-bridge",
            client_version=self.client_version,
            request_timeout=self.settings.request_timeout_seconds,
            max_message_bytes=self.settings.max_message_bytes,
        )

    async def _start_official_daemon(self) -> None:
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self.settings.codex_home)
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.codex_bin,
                "app-server",
                "daemon",
                "start",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except TimeoutError as exc:
            # Reap the timed-out child: leaving it running would orphan an
            # unmanaged process whose output pipes nobody drains.
            process.kill()
            await process.wait()
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex daemon start timed out",
            ) from exc
        except OSError as exc:
            raise BridgeError(
                "AGENT_RUNTIME_UNAVAILABLE",
                "Codex executable could not be started",
            ) from exc
        if process.returncode != 0:
            del stderr
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "official Codex daemon start failed",
            )
        # The lifecycle command is idempotent and returns only after the app
        # server is ready.  Its JSON stdout isn't part of the Bridge API.
        del stdout

    def _runtime_info(
        self,
        *,
        available: bool,
        version: str | None = None,
    ) -> RuntimeInfo:
        return RuntimeInfo(
            name=self.name,
            available=available,
            version=version,
            persistent_session=True,
            live_steer=True,
            interactive_approval=True,
            interactive_question=True,
            in_flight_recovery="session-resume",
        )


def _thread_id_from_result(result: Any) -> str:
    if isinstance(result, dict):
        thread = result.get("thread")
        if isinstance(thread, dict) and isinstance(thread.get("id"), str):
            return thread["id"]
    raise BridgeError("AGENT_PROVIDER_ERROR", "Codex returned no thread id")


def _turn_id_from_result(result: Any) -> str:
    if isinstance(result, dict):
        turn = result.get("turn")
        if isinstance(turn, dict) and isinstance(turn.get("id"), str):
            return turn["id"]
    raise BridgeError("AGENT_PROVIDER_ERROR", "Codex returned no turn id")


def _belongs_to_active(
    params: dict[str, Any],
    *,
    thread_id: str,
    turn_id: str,
) -> bool:
    event_thread = params.get("threadId")
    if isinstance(event_thread, str) and event_thread != thread_id:
        return False
    event_turn = params.get("turnId")
    if isinstance(event_turn, str) and event_turn != turn_id:
        return False
    turn = params.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str) and turn["id"] != turn_id:
        return False
    return True


def _provider_error_message(value: dict[str, Any]) -> str:
    for key in ("message", "error", "detail"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
        if isinstance(item, dict):
            nested = item.get("message")
            if isinstance(nested, str) and nested:
                return nested
    return "Codex App Server reported an error"


def _choose_final_message(messages: list[tuple[str | None, str]]) -> str:
    for phase, text in reversed(messages):
        if phase == "final_answer":
            return text
    if messages:
        return messages[-1][1]
    return ""


def _available_decisions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["approve_once", "deny", "cancel_task"]
    available: list[str] = []
    reverse = {native: public for public, native in _NATIVE_DECISIONS.items()}
    for item in value:
        if isinstance(item, str) and item in reverse:
            public = reverse[item]
            if public not in available:
                available.append(public)
    return available or ["approve_once", "deny", "cancel_task"]


def _native_decision(resolution: dict[str, Any]) -> str:
    decision = resolution.get("decision")
    if not isinstance(decision, str):
        return "decline"
    return _NATIVE_DECISIONS.get(decision, "decline")

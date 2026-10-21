"""Generic ACP v1 adapter. The ACP SDK is imported only when an ACP profile is configured."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..bootstrap import RuntimeProxy
from ..errors import BridgeError
from ..models import (
    AcpProfile,
    ReconciliationStatus,
    RuntimeInfo,
    TaskRecord,
)
from ..runtime_proxy import RuntimeProxyError, build_runtime_environment
from ..util import utc_now
from .acp_process import AcpChild, spawn_acp_child, stop_acp_child
from .base import AdapterResult, AgentAdapter, ReconcileResult, TaskContext

_TOOL_ID_LIMIT = 128
_TOOL_TITLE_LIMIT = 160
_TOOL_KINDS = {"read", "edit", "delete", "move", "search", "execute", "fetch", "other"}
_TOOL_STATUSES = {"pending", "in_progress", "completed", "failed"}


@dataclass
class _ActiveTask:
    connection: Any
    child: AcpChild
    stderr_task: asyncio.Task[None] | None = None
    session_id: str | None = None


class _Client:
    def __init__(self, context: TaskContext) -> None:
        self.context = context
        self.replaying = False
        self.text: list[str] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if self.replaying:
            return
        update_kind = getattr(update, "session_update", None)
        if update_kind in {"tool_call", "tool_call_update"}:
            payload = _tool_event_payload(update)
            if payload is not None and (
                update_kind == "tool_call" or payload.get("status") == "in_progress"
            ):
                await self.context.emit_event("tool.started", payload)
            return
        if update_kind != "agent_message_chunk":
            return
        content = getattr(update, "content", None)
        if getattr(content, "type", None) == "text" and isinstance(
            getattr(content, "text", None), str
        ):
            self.text.append(content.text)
            await self.context.emit_event("agent.message", {"text": content.text})

    async def request_permission(
        self, session_id: str, tool_call: Any, options: Any, **kwargs: Any
    ):
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        payload = _permission_payload(tool_call, options)
        try:
            resolution = await self.context.request_approval(payload)
        except (asyncio.CancelledError, BridgeError) as exc:
            if isinstance(exc, BridgeError) and exc.code not in {
                "AGENT_TASK_CANCELLED",
                "REQUEST_STALE",
            }:
                raise
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        option_id = resolution.get("option_id")
        if isinstance(option_id, str):
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id=option_id)
            )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


def _tool_event_payload(update: Any) -> dict[str, str] | None:
    tool_call_id = getattr(update, "tool_call_id", None)
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    payload = {"tool_call_id": tool_call_id[:_TOOL_ID_LIMIT]}
    title = getattr(update, "title", None)
    if isinstance(title, str) and title:
        payload["title"] = title[:_TOOL_TITLE_LIMIT]
    kind = getattr(update, "kind", None)
    if isinstance(kind, str) and kind in _TOOL_KINDS:
        payload["kind"] = kind
    status = getattr(update, "status", None)
    if isinstance(status, str) and status in _TOOL_STATUSES:
        payload["status"] = status
    return payload


class AcpAdapter(AgentAdapter):
    """One configured ACP executable, isolated to one child process per task."""

    def __init__(
        self,
        profile: AcpProfile,
        *,
        runtime_proxy: RuntimeProxy | None = None,
        load_capabilities: Callable[[str, str], dict[str, Any] | None] | None = None,
        save_capabilities: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.profile = profile
        self._runtime_proxy = runtime_proxy
        self._active: dict[str, _ActiveTask] = {}
        self._stopped: OrderedDict[str, None] = OrderedDict()
        self._stop_evidence_limit = 256
        self._load_supported = False
        self._interactive_approval = False
        self._save_capabilities = save_capabilities
        self._capability_snapshot: dict[str, Any] | None = None
        self._profile_fingerprint = _profile_fingerprint(profile)
        if load_capabilities is not None:
            snapshot = load_capabilities(profile.runtime_id, self._profile_fingerprint)
            if _valid_snapshot(snapshot):
                self._apply_snapshot(snapshot)

    @property
    def name(self) -> str:
        return self.profile.runtime_id

    async def probe(self) -> RuntimeInfo:
        executable = Path(self.profile.command)
        try:
            info = await asyncio.to_thread(executable.stat)
            available = stat.S_ISREG(info.st_mode) and os.access(executable, os.X_OK)
        except OSError:
            available = False
        persistent = self._load_supported
        return RuntimeInfo(
            name=self.name,
            available=available,
            version=(self._capability_snapshot or {}).get("agent_version"),
            persistent_session=persistent,
            live_steer=False,
            interactive_approval=self._interactive_approval,
            interactive_question=False,
            in_flight_recovery="session-resume" if persistent else "none",
            model_override=False,
            model_discovery="unsupported",
        )

    async def run_task(self, context: TaskContext) -> AdapterResult:
        return await self._run(context, resume=False)

    async def continue_task(self, context: TaskContext) -> AdapterResult:
        if not context.continue_native_session_id:
            raise BridgeError(
                "AGENT_SESSION_NOT_RESUMABLE", "ACP continuation requires a session id"
            )
        return await self._run(context, resume=True)

    async def _run(self, context: TaskContext, *, resume: bool) -> AdapterResult:
        import acp
        from acp.exceptions import RequestError
        from acp.schema import ClientCapabilities, FileSystemCapabilities, Implementation

        self._validate_executable()
        try:
            child_env = build_runtime_environment(
                os.environ,
                runtime=self.name,
                use_proxy=self.profile.use_proxy,
                proxy=self._runtime_proxy,
            )
        except RuntimeProxyError as exc:
            raise BridgeError("AGENT_PROVIDER_ERROR", str(exc)) from exc
        client = _Client(context)
        active: _ActiveTask | None = None
        try:
            child = await spawn_acp_child(
                self.profile.command,
                self.profile.args,
                env=child_env,
                cwd=context.cwd.resolve(),
            )
            process = child.process
            if process.stdin is None or process.stdout is None:
                raise RuntimeError("ACP stdio pipes were not created")
            connection = acp.connect_to_agent(client, process.stdin, process.stdout)
            stderr_task = asyncio.create_task(self._discard_stderr(process))
            active = _ActiveTask(connection, child, stderr_task)
            self._active[context.task_id] = active
            initialized = await asyncio.wait_for(
                connection.initialize(
                    protocol_version=acp.PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(
                        fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
                        terminal=False,
                    ),
                    client_info=Implementation(name="ServerFS Agent Bridge", version="0.19"),
                ),
                timeout=self.profile.probe_timeout_seconds,
            )
            if getattr(initialized, "protocol_version", None) != acp.PROTOCOL_VERSION:
                raise BridgeError(
                    "AGENT_PROVIDER_ERROR",
                    "ACP Agent negotiated an unsupported protocol version",
                )
            capabilities = getattr(initialized, "agent_capabilities", None)
            # ACP v1 defines request_permission as a stable Client method. Successful
            # initialize installs the SDK router that dispatches it to the callback above.
            self._interactive_approval = True
            # agent-client-protocol 0.12.1 pins schema v1.19.0, but its router explicitly
            # gates session/resume as unstable. Keep this adapter on stable ACP v1 and use only
            # the stable session/load route for continuation.
            self._load_supported = bool(getattr(capabilities, "load_session", False))
            agent_info = getattr(initialized, "agent_info", None)
            snapshot = {
                "protocol_version": _bounded_metadata(
                    getattr(initialized, "protocol_version", None)
                ),
                "agent_name": _bounded_metadata(getattr(agent_info, "name", None)),
                "agent_version": _bounded_metadata(getattr(agent_info, "version", None)),
                "load_session": self._load_supported,
                "interactive_approval": True,
                "measured_at": utc_now(),
            }
            self._apply_snapshot(snapshot)
            if self._save_capabilities is not None:
                self._save_capabilities(self.name, self._profile_fingerprint, snapshot)
            if resume:
                session_id = context.continue_native_session_id
                if self._load_supported:
                    client.replaying = True
                    try:
                        await connection.load_session(
                            cwd=str(context.cwd.resolve()),
                            session_id=session_id,
                            mcp_servers=[],
                        )
                    finally:
                        client.replaying = False
                else:
                    raise BridgeError(
                        "AGENT_SESSION_NOT_RESUMABLE",
                        "ACP Agent advertises no resume or load capability",
                    )
            else:
                session = await connection.new_session(
                    cwd=str(context.cwd.resolve()), mcp_servers=[]
                )
                session_id = session.session_id
            active.session_id = session_id
            await context.record_native_ids(session_id, None)
            response = await connection.prompt(session_id, [acp.text_block(context.prompt)])
            if getattr(response, "stop_reason", None) == "cancelled":
                raise BridgeError("AGENT_TASK_CANCELLED", "ACP Agent cancelled the task")
            result = AdapterResult(
                final_response="".join(client.text), native_session_id=session_id
            )
            return result
        except BridgeError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, RequestError) and exc.code == RequestError.auth_required().code:
                raise BridgeError(
                    "AGENT_AUTH_REQUIRED", "ACP Agent authentication is required"
                ) from exc
            # Protocol errors can include peer data; expose only a bounded generic failure.
            raise BridgeError(
                "AGENT_PROVIDER_ERROR", "ACP Agent protocol or process failure"
            ) from exc
        finally:
            if active is not None:
                try:
                    if active.session_id is not None:
                        try:
                            await asyncio.wait_for(active.connection.cancel(active.session_id), 0.5)
                        except Exception:
                            pass
                    try:
                        await asyncio.wait_for(active.connection.close(), 0.5)
                    except Exception:
                        pass
                    try:
                        active.child.process.stdin.close()
                    except (AttributeError, OSError):
                        pass
                    stopped = await asyncio.shield(
                        stop_acp_child(active.child, self.profile.shutdown_timeout_seconds)
                    )
                    if not stopped:
                        raise BridgeError(
                            "AGENT_PROVIDER_STOP_UNKNOWN",
                            "ACP child process group did not stop",
                        )
                    self._remember_stopped(context.task_id)
                finally:
                    self._active.pop(context.task_id, None)
                    if active.stderr_task is not None:
                        active.stderr_task.cancel()
                        await asyncio.gather(active.stderr_task, return_exceptions=True)

    def _remember_stopped(self, task_id: str) -> None:
        self._stopped[task_id] = None
        self._stopped.move_to_end(task_id)
        while len(self._stopped) > self._stop_evidence_limit:
            self._stopped.popitem(last=False)

    @staticmethod
    async def _discard_stderr(process: Any) -> None:
        """Drain provider stderr so it cannot block the child; never publish its contents."""
        stderr = process.stderr
        if stderr is not None:
            while await stderr.read(4096):
                pass

    def _validate_executable(self) -> None:
        path = Path(self.profile.command)
        try:
            info = path.stat()
        except OSError as exc:
            raise BridgeError(
                "AGENT_RUNTIME_UNAVAILABLE", "Configured ACP executable is unavailable"
            ) from exc
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
            raise BridgeError(
                "AGENT_RUNTIME_UNAVAILABLE", "Configured ACP executable is not executable"
            )

    async def get_state(self, task_id: str) -> str | None:
        return "running" if task_id in self._active else None

    async def send_message(self, task_id: str, message: str) -> None:
        raise BridgeError("AGENT_PROVIDER_ERROR", "ACP live steering is unsupported")

    async def cancel(self, task_id: str) -> None:
        active = self._active.get(task_id)
        if active is None:
            return
        try:
            if active.session_id is not None:
                await asyncio.wait_for(active.connection.cancel(active.session_id), 0.5)
        except Exception:
            pass
        stopped = await asyncio.shield(
            stop_acp_child(active.child, self.profile.shutdown_timeout_seconds)
        )
        if not stopped:
            raise BridgeError("AGENT_PROVIDER_STOP_UNKNOWN", "ACP child process group did not stop")
        self._remember_stopped(task_id)

    async def reconcile_task(self, task: TaskRecord) -> ReconcileResult:
        stopped = getattr(task, "task_id", None) in self._stopped
        return ReconcileResult(
            status=(
                ReconciliationStatus.SESSION_RESUMABLE
                if task.native_session_id is not None and self._load_supported
                else ReconciliationStatus.UNKNOWN
            ),
            provider_active=False if stopped else None,
            detail=(
                "ACP task-owned process group was stopped by this adapter instance"
                if stopped
                else "ACP process state is unknown; a persisted session may be resumable"
            ),
        )

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._capability_snapshot = snapshot
        self._load_supported = snapshot["load_session"]
        self._interactive_approval = snapshot["interactive_approval"]


def _profile_fingerprint(profile: AcpProfile) -> str:
    fields = {
        "profile_id": profile.id,
        "runtime_id": profile.runtime_id,
        "command": profile.command,
        "args": profile.args,
        "use_proxy": profile.use_proxy,
        "probe_timeout_seconds": profile.probe_timeout_seconds,
        "shutdown_timeout_seconds": profile.shutdown_timeout_seconds,
    }
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_metadata(value: Any) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 2**31 - 1:
        return value
    if isinstance(value, str) and value.strip() and len(value.encode("utf-8")) <= 128:
        return value.strip()
    return None


def _valid_snapshot(snapshot: Any) -> bool:
    return (
        isinstance(snapshot, dict)
        and isinstance(snapshot.get("load_session"), bool)
        and snapshot.get("interactive_approval") is True
        and isinstance(snapshot.get("measured_at"), str)
        and _bounded_metadata(snapshot.get("protocol_version")) is not None
        and (
            snapshot.get("agent_name") is None
            or _bounded_metadata(snapshot.get("agent_name")) is not None
        )
        and (
            snapshot.get("agent_version") is None
            or _bounded_metadata(snapshot.get("agent_version")) is not None
        )
    )


_MAX_PERMISSION_OPTIONS = 16
_MAX_PERMISSION_TEXT = 256
_PERMISSION_KINDS = {"allow_once", "allow_always", "reject_once", "reject_always"}


def _permission_payload(tool_call: Any, options: Any) -> dict[str, Any]:
    """Project ACP permission data onto the bounded public approval contract."""
    if not isinstance(options, (list, tuple)) or not 1 <= len(options) <= _MAX_PERMISSION_OPTIONS:
        raise BridgeError("AGENT_PROVIDER_ERROR", "ACP permission options are invalid")
    title = _bounded_text(getattr(tool_call, "title", None), "ACP permission request")
    if title == "ACP permission request":
        title = _bounded_text(getattr(tool_call, "name", None), title)
    normalized = []
    seen: set[str] = set()
    for option in options:
        option_id = getattr(option, "option_id", None)
        name = getattr(option, "name", None)
        kind = getattr(option, "kind", None)
        if (
            not isinstance(option_id, str)
            or not option_id
            or len(option_id.encode("utf-8")) > _MAX_PERMISSION_TEXT
            or option_id in seen
            or not isinstance(name, str)
            or not name.strip()
            or len(name.encode("utf-8")) > _MAX_PERMISSION_TEXT
            or not isinstance(kind, str)
            or not kind
            or len(kind.encode("utf-8")) > _MAX_PERMISSION_TEXT
        ):
            raise BridgeError("AGENT_PROVIDER_ERROR", "ACP permission options are invalid")
        seen.add(option_id)
        normalized.append(
            {
                "option_id": option_id,
                "label": name.strip(),
                "kind": kind if kind in _PERMISSION_KINDS else "other",
            }
        )
    return {"category": "provider_permission", "title": title, "response_options": normalized}


def _bounded_text(value: Any, fallback: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > _MAX_PERMISSION_TEXT
    ):
        return fallback
    return value.strip()


def _bounded_text(value: Any, fallback: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > _MAX_PERMISSION_TEXT
    ):
        return fallback
    return value.strip()

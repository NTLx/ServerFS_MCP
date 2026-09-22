"""Public MCP tools that proxy provider-neutral Agent Bridge RPC methods."""

from __future__ import annotations

import contextlib
import time
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import logging as jsonlog
from .agent_client import (
    AgentBridgeClient,
    AgentBridgeClientError,
    AgentBridgeRemoteError,
    AgentBridgeUnavailable,
)
from .config import Settings
from .fdio import open_directory_fd, root_fd
from .models import AgentQuestionAnswer
from .paths import PathSecurityError, resolve_workdir_path
from .tools import deny_policy_from_workdir
from .workdirs import (
    AGENT_MODE_DISABLED,
    AGENT_MODE_REVIEW,
    AGENT_MODE_WORKSPACE_WRITE,
    Workdir,
    WorkdirRegistry,
)

AGENT_READ_ANNOTATIONS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
AGENT_SUBMIT_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)
AGENT_APPROVAL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)
AGENT_QUESTION_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
AGENT_MESSAGE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)
AGENT_CANCEL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)

RuntimeArg = Annotated[
    Literal["codex", "claude"],
    Field(description="Configured native Agent runtime"),
]
AgentProfileArg = Annotated[
    Literal["review", "workspace-write"],
    Field(
        description=(
            "Provider-neutral execution profile; native Codex/Claude require workspace-write"
        )
    ),
]


def register_agent_tools(
    mcp: MCPServer,
    registry: WorkdirRegistry,
    settings: Settings,
    client: AgentBridgeClient,
) -> None:
    """Register the eight v0.3 Agent delegation tools."""

    @mcp.tool(annotations=AGENT_READ_ANNOTATIONS)
    async def list_agent_runtimes() -> dict[str, Any]:
        """List configured Agent runtimes and their current normalized capabilities.

        This is read-only and never starts or installs a provider. Runtimes not
        allowlisted on any ServerFS workdir are omitted.
        """
        t0 = time.monotonic()
        try:
            allowed = _allowed_runtimes(registry)
            if not allowed:
                output = {"runtimes": []}
            else:
                result = await client.call("runtime.list", {})
                runtimes = result.get("runtimes", [])
                if not isinstance(runtimes, list):
                    raise ToolError("AGENT_BRIDGE_PROTOCOL_ERROR: invalid runtime list")
                filtered = [
                    item
                    for item in runtimes
                    if isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and item["name"] in allowed
                ]
                output = {"runtimes": filtered}
        except Exception as exc:
            err = _agent_tool_error(exc)
            _audit_agent(
                "list_agent_runtimes",
                t0,
                success=False,
                error_code=_tool_error_code(err),
            )
            raise err from exc
        _audit_agent(
            "list_agent_runtimes",
            t0,
            success=True,
            returned=len(output["runtimes"]),
        )
        return output

    @mcp.tool(annotations=AGENT_SUBMIT_ANNOTATIONS)
    async def submit_agent_task(
        runtime: RuntimeArg,
        workdir: Annotated[str, Field(description="Workdir alias where delegation starts")],
        prompt: Annotated[
            str,
            Field(
                description=(
                    "One narrow authorized objective for the delegated Agent. Include only "
                    "the context needed to complete that objective, state the allowed mutation "
                    "scope and stop conditions, and prefer structured project actions over "
                    "unrelated implementation detail."
                )
            ),
        ],
        path: Annotated[
            str, Field(default="", description="Relative starting directory inside the workdir")
        ] = "",
        profile: AgentProfileArg = "workspace-write",
        continue_from_task_id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Prior terminal ServerFS Agent task whose native provider session should resume"
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Submit one narrow authorized Agent objective and return a task handle.

        Keep the task atomic: provide only the context required for the objective,
        explicitly bound allowed mutations and stop conditions, and prefer existing
        structured project workflows over embedding unrelated shell/network/security
        implementation detail. This guidance reduces ambiguity and accidental scope
        expansion; it does not weaken or bypass provider safety checks.

        The call never waits for provider completion. Poll get_agent_task or
        read_agent_task_events. A follow-up conversation uses a NEW task with
        continue_from_task_id.
        """
        t0 = time.monotonic()
        try:
            wd = _authorize_submit(registry, workdir, runtime, profile)
            normalized_path = _validate_agent_cwd(wd, path, settings)
            params: dict[str, Any] = {
                "runtime": runtime,
                "workdir": workdir,
                "path": normalized_path,
                "profile": profile,
                "prompt": prompt,
            }
            if continue_from_task_id is not None:
                params["continue_from_task_id"] = continue_from_task_id
            result = await client.call("task.submit", params)
        except Exception as exc:
            err = _agent_tool_error(exc)
            _audit_agent(
                "submit_agent_task",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                runtime=runtime,
                profile=profile,
                error_code=_tool_error_code(err),
            )
            raise err from exc
        _audit_agent(
            "submit_agent_task",
            t0,
            success=True,
            workdir=workdir,
            path=normalized_path,
            runtime=runtime,
            profile=profile,
            task_id=result.get("task_id"),
            status=result.get("status"),
        )
        return result

    @mcp.tool(annotations=AGENT_READ_ANNOTATIONS)
    async def get_agent_task(
        task_id: Annotated[str, Field(description="ServerFS Agent task identifier")],
    ) -> dict[str, Any]:
        """Get the normalized state/result or pending interaction for one Agent task."""
        return await _simple_agent_call(
            client,
            "get_agent_task",
            "task.get",
            {"task_id": task_id},
            task_id=task_id,
        )

    @mcp.tool(annotations=AGENT_READ_ANNOTATIONS)
    async def read_agent_task_events(
        task_id: Annotated[str, Field(description="ServerFS Agent task identifier")],
        after_event_id: Annotated[
            int, Field(default=0, ge=0, description="Return events after this cursor")
        ] = 0,
        limit: Annotated[
            int, Field(default=100, ge=1, le=500, description="Maximum normalized events")
        ] = 100,
    ) -> dict[str, Any]:
        """Read cursor-paginated normalized task events; never raw provider stdout/stderr."""
        return await _simple_agent_call(
            client,
            "read_agent_task_events",
            "task.events",
            {"task_id": task_id, "after_event_id": after_event_id, "limit": limit},
            task_id=task_id,
        )

    @mcp.tool(annotations=AGENT_APPROVAL_ANNOTATIONS)
    async def respond_agent_approval(
        task_id: Annotated[str, Field(description="ServerFS Agent task identifier")],
        request_id: Annotated[str, Field(description="Pending approval request identifier")],
        decision: Annotated[
            Literal["approve_once", "approve_session", "deny", "cancel_task"],
            Field(description="Decision offered by the pending runtime approval"),
        ],
        granted_permission_ids: Annotated[
            list[str] | None,
            Field(
                default=None,
                description="Optional subset of permission IDs from the pending request",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Resolve one pending Agent approval; no permanent approvals are exposed."""
        params: dict[str, Any] = {
            "task_id": task_id,
            "request_id": request_id,
            "decision": decision,
        }
        if granted_permission_ids is not None:
            params["granted_permission_ids"] = granted_permission_ids
        return await _simple_agent_call(
            client,
            "respond_agent_approval",
            "task.approval.respond",
            params,
            task_id=task_id,
            request_id=request_id,
            decision=decision,
        )

    @mcp.tool(annotations=AGENT_QUESTION_ANNOTATIONS)
    async def answer_agent_question(
        task_id: Annotated[str, Field(description="ServerFS Agent task identifier")],
        request_id: Annotated[str, Field(description="Pending question request identifier")],
        answers: Annotated[
            list[AgentQuestionAnswer],
            Field(min_length=1, description="One answer for every pending question"),
        ],
    ) -> dict[str, Any]:
        """Answer one pending Agent question set.

        Supports option selection, multiple choice and free text as represented
        by the provider-neutral pending request returned by get_agent_task.
        """
        return await _simple_agent_call(
            client,
            "answer_agent_question",
            "task.question.answer",
            {
                "task_id": task_id,
                "request_id": request_id,
                "answers": [answer.model_dump(exclude_none=True) for answer in answers],
            },
            task_id=task_id,
            request_id=request_id,
            answer_count=len(answers),
        )

    @mcp.tool(annotations=AGENT_MESSAGE_ANNOTATIONS)
    async def send_agent_message(
        task_id: Annotated[str, Field(description="Active ServerFS Agent task identifier")],
        message: Annotated[
            str,
            Field(
                description=(
                    "Narrow follow-up for the active task; include only information required "
                    "to steer the current objective without expanding its authorized scope"
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Steer an active task with a narrow follow-up when live_steer is supported.

        Keep steering within the task's existing authorized objective and mutation scope.
        If the requested work is a distinct objective, submit a new atomic task instead.
        For a terminal task, submit a new task with continue_from_task_id. Runtimes such as
        Claude may reject live steering even while active.
        """
        return await _simple_agent_call(
            client,
            "send_agent_message",
            "task.message.send",
            {"task_id": task_id, "message": message},
            task_id=task_id,
            message_bytes=len(message.encode("utf-8")),
        )

    @mcp.tool(annotations=AGENT_CANCEL_ANNOTATIONS)
    async def cancel_agent_task(
        task_id: Annotated[str, Field(description="ServerFS Agent task identifier")],
    ) -> dict[str, Any]:
        """Cancel or interrupt an active Agent task; repeated cancellation is safe."""
        return await _simple_agent_call(
            client,
            "cancel_agent_task",
            "task.cancel",
            {"task_id": task_id},
            task_id=task_id,
        )


async def _simple_agent_call(
    client: AgentBridgeClient,
    tool: str,
    method: str,
    params: dict[str, Any],
    **audit_fields: object,
) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        result = await client.call(method, params)
    except Exception as exc:
        err = _agent_tool_error(exc)
        _audit_agent(
            tool,
            t0,
            success=False,
            error_code=_tool_error_code(err),
            **audit_fields,
        )
        raise err from exc
    _audit_agent(tool, t0, success=True, **audit_fields)
    return result


def _authorize_submit(
    registry: WorkdirRegistry,
    workdir: str,
    runtime: str,
    profile: str,
) -> Workdir:
    wd = registry.get(workdir)
    if wd is None:
        raise ToolError(f"WORKDIR_NOT_FOUND: {workdir!r} is not a configured workdir")
    if wd.agent_mode == AGENT_MODE_DISABLED:
        raise ToolError(f"AGENT_DISABLED: Agent delegation is disabled for {workdir}")
    if runtime not in wd.agent_runtimes:
        raise ToolError(
            f"AGENT_RUNTIME_NOT_ALLOWED: runtime {runtime!r} is not allowed for {workdir}"
        )

    rank = {
        AGENT_MODE_REVIEW: 1,
        AGENT_MODE_WORKSPACE_WRITE: 2,
    }
    if profile not in rank or rank[profile] > rank.get(wd.agent_mode, 0):
        raise ToolError(
            f"AGENT_PROFILE_NOT_ALLOWED: profile {profile!r} is not allowed for {workdir}"
        )
    if runtime in {"codex", "claude"} and profile != AGENT_MODE_WORKSPACE_WRITE:
        raise ToolError(
            f"AGENT_PROFILE_NOT_ALLOWED: native {runtime} currently requires workspace-write"
        )
    return wd


def _validate_agent_cwd(wd: Workdir, path: str, settings: Settings) -> str:
    try:
        resolved = resolve_workdir_path(
            wd,
            path,
            allow_hidden=wd.policy.allow_hidden,
            deny_policy=deny_policy_from_workdir(wd),
        )
        with contextlib.ExitStack() as stack:
            root = stack.enter_context(root_fd(str(wd.container_path)))
            stack.enter_context(open_directory_fd(root, resolved.rel_parts))
        return resolved.rel_path
    except PathSecurityError as exc:
        raise ToolError(f"{exc.code}: {wd.alias}:{path} — {exc.message}") from exc
    except FileNotFoundError as exc:
        raise ToolError(f"PATH_NOT_FOUND: {wd.alias}:{path} does not exist") from exc
    except NotADirectoryError as exc:
        raise ToolError(f"NOT_A_DIRECTORY: {wd.alias}:{path} is not a directory") from exc
    except OSError as exc:
        raise ToolError(f"ACCESS_DENIED: {wd.alias}:{path} ({exc.strerror})") from exc


def _allowed_runtimes(registry: WorkdirRegistry) -> set[str]:
    allowed: set[str] = set()
    for wd in registry.all_workdirs():
        allowed.update(wd.agent_runtimes)
    return allowed


def _agent_tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, AgentBridgeRemoteError):
        return ToolError(f"{exc.code}: {exc.message}")
    if isinstance(exc, AgentBridgeUnavailable):
        return ToolError("AGENT_BRIDGE_UNAVAILABLE: Agent Bridge is unavailable")
    if isinstance(exc, AgentBridgeClientError):
        return ToolError("AGENT_BRIDGE_PROTOCOL_ERROR: Agent Bridge protocol failure")
    return ToolError("AGENT_BRIDGE_ERROR: Agent Bridge request failed")


def _audit_agent(
    tool: str,
    t0: float,
    *,
    success: bool,
    error_code: str | None = None,
    **fields: object,
) -> None:
    record: dict[str, object] = {
        "tool": tool,
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
        "success": success,
    }
    if error_code is not None:
        record["error_code"] = error_code
    record.update(fields)
    jsonlog.info("tool_call", **record)


def _tool_error_code(exc: ToolError) -> str:
    return str(exc).split(":", 1)[0].strip()

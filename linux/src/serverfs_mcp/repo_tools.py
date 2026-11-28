"""Public deterministic repository tools backed by host-side Repo Ops RPC.

Repo Ops reuses the authenticated Bridge transport but never submits an Agent task or
invokes a provider runtime. The caller cannot supply an executable, argv, environment,
Git remote/ref, force option, or shell fragment.
"""

from __future__ import annotations

import re
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
from .paths import PathSecurityError, resolve_workdir_path
from .tools import deny_policy_from_workdir
from .workdirs import Workdir, WorkdirRegistry

REPO_READ_ANNOTATIONS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
REPO_ACTION_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)
REPO_COMMIT_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)
REPO_PUSH_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

RepoViewArg = Annotated[
    Literal["status", "diff", "staged_diff", "actions"],
    Field(description="Repository view to inspect"),
]


def register_repo_tools(
    mcp: MCPServer,
    registry: WorkdirRegistry,
    settings: Settings,
    client: AgentBridgeClient,
) -> None:
    """Register four deterministic non-Agent repository tools."""

    @mcp.tool(annotations=REPO_READ_ANNOTATIONS)
    async def inspect_repo(
        workdir: Annotated[str, Field(description="Repo Ops-enabled workdir alias")],
        view: RepoViewArg,
        paths: Annotated[
            list[str] | None,
            Field(
                default=None,
                max_length=64,
                description="Optional explicit workdir-relative file paths for diff views",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Inspect Git status/diff or list administrator-configured Repo Ops actions.

        This never starts an Agent runtime. `status` and `actions` accept no paths;
        `diff` and `staged_diff` require explicit policy-valid paths. Status path lists
        are filtered through the ServerFS path/deny policy before they are returned.
        Output is bounded by the host Repo Ops service.
        """
        t0 = time.monotonic()
        try:
            wd = _authorize_repo_workdir(registry, workdir)
            requested_paths = paths or []
            if view in {"status", "actions"} and requested_paths:
                raise ToolError(f"INVALID_REQUEST: {view} view does not accept paths")
            if view in {"diff", "staged_diff"} and not requested_paths:
                raise ToolError(f"INVALID_REQUEST: {view} requires at least one explicit path")
            normalized = [_validate_repo_path(wd, path) for path in requested_paths]
            if len(normalized) != len(set(normalized)):
                raise ToolError("INVALID_REQUEST: repo paths must be unique")
            result = await client.call(
                "repo.inspect",
                {"workdir": workdir, "view": view, "paths": normalized},
            )
            if view == "status":
                result = _filter_status_paths(wd, result)
        except Exception as exc:
            err = _repo_tool_error(exc)
            _audit_repo(
                "inspect_repo",
                t0,
                success=False,
                workdir=workdir,
                view=view,
                error_code=_tool_error_code(err),
            )
            raise err from exc
        _audit_repo(
            "inspect_repo",
            t0,
            success=True,
            workdir=workdir,
            view=view,
            path_count=len(paths or []),
        )
        return result

    @mcp.tool(annotations=REPO_ACTION_ANNOTATIONS)
    async def run_repo_action(
        workdir: Annotated[str, Field(description="Repo Ops-enabled workdir alias")],
        action: Annotated[
            str,
            Field(
                min_length=1,
                max_length=64,
                pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$",
                description="Administrator-configured named repository action",
            ),
        ],
        path: Annotated[
            str | None,
            Field(
                default=None,
                description="Optional workdir-relative path accepted only by path actions",
            ),
        ] = None,
        release_tag: Annotated[
            str | None,
            Field(
                default=None,
                max_length=128,
                description="Release action only: vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-beta.N",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Run one fixed administrator-configured repository action.

        The executable and base argv are not request parameters. No shell is used. Named
        actions require a read-write workdir and always take the host writer lease, including
        logically read-only checks, so integrity verification cannot race ServerFS/Agent writes.
        A path is accepted only when that action declares a path argument and must pass the
        same ServerFS path policy as filesystem tools. Built-in release actions instead
        accept only release_tag, validate semver, and fix HEAD/repository/workflow internally.
        Unknown parameters such as force, commit, remote or workflow are rejected.
        """
        t0 = time.monotonic()
        try:
            wd = _authorize_repo_workdir(registry, workdir, mutable=True)
            normalized_path = _validate_repo_path(wd, path) if path is not None else None
            params: dict[str, Any] = {"workdir": workdir, "action": action, "path": normalized_path}
            if release_tag is not None:
                if (
                    path is not None
                    or len(release_tag) > 128
                    or re.fullmatch(
                        r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-beta\.(?:0|[1-9][0-9]*))?",
                        release_tag,
                    )
                    is None
                ):
                    raise ToolError("INVALID_RELEASE_TAG: expected a semver release tag only")
                params["release_tag"] = release_tag
            result = await client.call("repo.action", params)
        except Exception as exc:
            err = _repo_tool_error(exc)
            _audit_repo(
                "run_repo_action",
                t0,
                success=False,
                workdir=workdir,
                action=action,
                error_code=_tool_error_code(err),
            )
            raise err from exc
        _audit_repo(
            "run_repo_action",
            t0,
            success=True,
            workdir=workdir,
            action=action,
            mutates=result.get("mutates"),
        )
        return result

    @mcp.tool(annotations=REPO_COMMIT_ANNOTATIONS)
    async def commit_repo_changes(
        workdir: Annotated[str, Field(description="Repo Ops-enabled read-write workdir alias")],
        paths: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=64,
                description="Exact workdir-relative file paths to stage and commit",
            ),
        ],
        message: Annotated[
            str,
            Field(
                min_length=1,
                max_length=1024,
                description="Single-line Git commit message",
            ),
        ],
    ) -> dict[str, Any]:
        """Stage only explicit files and create one Git commit; never push.

        The operation refuses pre-existing staged changes and directory paths so an MCP
        caller cannot accidentally sweep unrelated work into the commit. Git hooks are
        disabled by the host Repo Ops implementation for this deterministic path.
        """
        t0 = time.monotonic()
        try:
            wd = _authorize_repo_workdir(registry, workdir, mutable=True)
            normalized = [_validate_repo_path(wd, path) for path in paths]
            if len(normalized) != len(set(normalized)):
                raise ToolError("INVALID_REQUEST: repo paths must be unique")
            if any(char in message for char in ("\x00", "\n", "\r")):
                raise ToolError("INVALID_REQUEST: commit message must be one line")
            result = await client.call(
                "repo.commit",
                {"workdir": workdir, "paths": normalized, "message": message},
            )
        except Exception as exc:
            err = _repo_tool_error(exc)
            _audit_repo(
                "commit_repo_changes",
                t0,
                success=False,
                workdir=workdir,
                path_count=len(paths),
                error_code=_tool_error_code(err),
            )
            raise err from exc
        _audit_repo(
            "commit_repo_changes",
            t0,
            success=True,
            workdir=workdir,
            path_count=len(paths),
            commit=result.get("commit"),
        )
        return result

    @mcp.tool(annotations=REPO_PUSH_ANNOTATIONS)
    async def push_repo(
        workdir: Annotated[str, Field(description="Repo Ops-enabled read-write workdir alias")],
    ) -> dict[str, Any]:
        """Push exactly the current branch to its configured upstream branch.

        The host derives the upstream remote/ref and supplies an exact fixed refspec. No remote,
        ref, force option, credential or arbitrary Git argument is accepted from the caller.
        This never starts an Agent runtime.
        """
        t0 = time.monotonic()
        try:
            _authorize_repo_workdir(registry, workdir, mutable=True)
            result = await client.call("repo.push", {"workdir": workdir})
        except Exception as exc:
            err = _repo_tool_error(exc)
            _audit_repo(
                "push_repo",
                t0,
                success=False,
                workdir=workdir,
                error_code=_tool_error_code(err),
            )
            raise err from exc
        _audit_repo("push_repo", t0, success=True, workdir=workdir)
        return result

    # The pinned MCP SDK defaults to ignoring unknown arguments. Release operations
    # must reject caller attempts to supply force/commit/workflow, not ignore them.
    action_tool = mcp._tool_manager.get_tool("run_repo_action")
    assert action_tool is not None
    model = action_tool.fn_metadata.arg_model
    model.model_config["extra"] = "forbid"
    model.model_rebuild(force=True)
    action_tool.parameters["additionalProperties"] = False


def _authorize_repo_workdir(
    registry: WorkdirRegistry,
    alias: str,
    *,
    mutable: bool = False,
) -> Workdir:
    wd = registry.get(alias)
    if wd is None:
        raise ToolError(f"WORKDIR_NOT_FOUND: {alias!r} is not a configured workdir")
    if not wd.policy.repo_ops_enabled:
        raise ToolError(f"REPO_OPS_DISABLED: Repo Ops is disabled for {alias!r}")
    if mutable and wd.read_only:
        raise ToolError(f"WORKDIR_READ_ONLY: {alias!r} does not allow repository mutations")
    return wd


def _filter_status_paths(workdir: Workdir, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("view") != "status":
        raise ToolError("REPO_OPS_PROTOCOL_ERROR: invalid repository status response")
    head = result.get("head")
    branch = result.get("branch")
    upstream = result.get("upstream")
    if not isinstance(head, str) or not head:
        raise ToolError("REPO_OPS_PROTOCOL_ERROR: repository status is missing HEAD")
    if branch is not None and not isinstance(branch, str):
        raise ToolError("REPO_OPS_PROTOCOL_ERROR: repository branch has an invalid type")
    if upstream is not None and not isinstance(upstream, str):
        raise ToolError("REPO_OPS_PROTOCOL_ERROR: repository upstream has an invalid type")

    filtered: dict[str, Any] = {
        "view": "status",
        "head": head,
        "branch": branch,
        "upstream": upstream,
    }
    restricted = False
    dirty = False
    for key in ("unstaged_paths", "staged_paths", "untracked_paths"):
        values = result.get(key)
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ToolError("REPO_OPS_PROTOCOL_ERROR: repository path list has an invalid type")
        dirty = dirty or bool(values)
        allowed: list[str] = []
        for value in values:
            try:
                allowed.append(_validate_repo_path(workdir, value))
            except ToolError:
                restricted = True
        filtered[key] = allowed
    filtered["dirty"] = dirty
    filtered["restricted_changes_present"] = restricted
    return filtered


def _validate_repo_path(workdir: Workdir, path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ToolError("INVALID_REPO_PATH: repo path must be a non-empty string")
    try:
        resolved = resolve_workdir_path(
            workdir,
            path,
            allow_hidden=workdir.policy.allow_hidden,
            deny_policy=deny_policy_from_workdir(workdir),
        )
    except PathSecurityError as exc:
        raise ToolError(f"{exc.code}: repo path is not allowed") from exc
    if not resolved.rel_path:
        raise ToolError("INVALID_REPO_PATH: repo path must name an entry")
    return resolved.rel_path


def _repo_tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, AgentBridgeRemoteError):
        return ToolError(f"{exc.code}: {exc.message}")
    if isinstance(exc, AgentBridgeUnavailable):
        return ToolError("REPO_OPS_UNAVAILABLE: host Repo Ops service is unavailable")
    if isinstance(exc, AgentBridgeClientError):
        return ToolError("REPO_OPS_PROTOCOL_ERROR: host Repo Ops protocol failure")
    return ToolError("REPO_OPS_ERROR: repository operation failed")


def _audit_repo(
    tool: str,
    t0: float,
    *,
    success: bool,
    error_code: str | None = None,
    **fields: object,
) -> None:
    record: dict[str, object] = {
        "schema_version": 1,
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

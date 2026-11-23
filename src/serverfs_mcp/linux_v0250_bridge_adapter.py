"""Adapt existing Agent and Repo Ops calls to a grant-scoped Core Bridge v2 client."""

from __future__ import annotations

import re
import sys
from typing import Any

from .agent_client import (
    AgentBridgeClientError,
    AgentBridgeRemoteError,
    AgentBridgeUnavailable,
)
from .core_bridge_client_v2 import (
    CoreBridgeClientV2Error,
    CoreBridgeRemoteError,
    CoreBridgeUnavailable,
)

_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_MESSAGE = re.compile(r"[A-Za-z0-9 _.,:;()/'-]{1,256}\Z")
_PARAMS = {
    "bridge.ping": frozenset(),
    "runtime.list": frozenset(),
    "runtime.models": frozenset({"runtime", "workdir", "task_prompt", "path", "profile"}),
    "task.submit": frozenset(
        {
            "runtime",
            "workdir",
            "path",
            "profile",
            "prompt",
            "model",
            "continue_from_task_id",
            "correlation_id",
            "idempotency_key",
        }
    ),
    "task.get": frozenset({"task_id", "workdir"}),
    "task.events": frozenset({"task_id", "after_event_id", "limit", "workdir"}),
    "task.result.read": frozenset({"task_id", "offset_bytes", "max_bytes", "workdir"}),
    "task.cancel": frozenset({"task_id", "workdir"}),
    "task.message.send": frozenset({"task_id", "message", "workdir"}),
    "task.approval.respond": frozenset(
        {"task_id", "request_id", "decision", "option_id", "granted_permission_ids", "workdir"}
    ),
    "task.question.answer": frozenset({"task_id", "request_id", "answers", "workdir"}),
    "repo.inspect": frozenset({"workdir", "view", "paths"}),
    "repo.action": frozenset({"workdir", "action", "path", "release_tag"}),
    "repo.commit": frozenset({"workdir", "paths", "message"}),
    "repo.push": frozenset({"workdir"}),
}


class LinuxV0250BridgeAdapter:
    """Expose only existing Agent/Repo RPCs through one immutable v2 grant."""

    def __init__(self, client: Any, workdir_alias: str) -> None:
        if sys.platform != "linux":
            raise ValueError("Linux Core Bridge adapter is available only on Linux")
        if not callable(getattr(client, "call", None)):
            raise ValueError("Core Bridge client is invalid")
        if not isinstance(workdir_alias, str) or not _ALIAS.fullmatch(workdir_alias):
            raise ValueError("Core Bridge workdir grant is invalid")
        if getattr(client, "_workdir_alias", None) != workdir_alias:
            raise ValueError("Core Bridge client grant does not match adapter workdir")
        self._client = client
        self._workdir_alias = workdir_alias

    def __repr__(self) -> str:
        return "LinuxV0250BridgeAdapter(<grant credentials hidden>)"

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(method, str) or method not in _PARAMS:
            raise AgentBridgeClientError("unsupported Agent Bridge method")
        if not isinstance(params, dict) or any(key not in _PARAMS[method] for key in params):
            raise AgentBridgeClientError("invalid Agent Bridge parameters")
        forwarded = dict(params)
        if "workdir" in forwarded:
            if forwarded["workdir"] != self._workdir_alias:
                raise AgentBridgeClientError("workdir is outside the authorized grant")
            del forwarded["workdir"]
        try:
            return await self._client.call(method, forwarded)
        except CoreBridgeRemoteError as exc:
            code, message = exc.code, exc.message
            if (
                not isinstance(code, str)
                or not _CODE.fullmatch(code)
                or not isinstance(message, str)
                or not _MESSAGE.fullmatch(message)
            ):
                code, message = "CORE_BRIDGE_REMOTE_ERROR", "remote request failed"
            elif any(secret and secret in message for secret in _credentials(self._client)):
                message = "remote request failed"
            raise AgentBridgeRemoteError(code, message) from None
        except CoreBridgeUnavailable:
            raise AgentBridgeUnavailable("Core Bridge is unavailable") from None
        except CoreBridgeClientV2Error:
            raise AgentBridgeClientError("Core Bridge protocol error") from None


_TASK_OWNED_METHODS = frozenset(
    {
        "task.get",
        "task.events",
        "task.result.read",
        "task.cancel",
        "task.message.send",
        "task.approval.respond",
        "task.question.answer",
    }
)


class LinuxV026MultiBridgeAdapter:
    """Route each signed call through its exact authorized workdir identity.

    Task-only MCP methods expose no workdir parameter. Probe task ownership via
    authenticated task.get, then dispatch; no untrusted task-id alias inference
    or volatile task ownership cache is required after a Core restart.
    """

    def __init__(self, clients: dict[str, Any]) -> None:
        if sys.platform != "linux" or not isinstance(clients, dict) or not 2 <= len(clients) <= 16:
            raise ValueError("Core Bridge multi-alias grant is invalid")
        if any(
            getattr(client, "_workdir_alias", None) != alias for alias, client in clients.items()
        ):
            raise ValueError("Core Bridge client grant does not match adapter workdir")
        self._adapters = {
            alias: LinuxV0250BridgeAdapter(client, alias) for alias, client in clients.items()
        }

    def __repr__(self) -> str:
        return "LinuxV026MultiBridgeAdapter(<grant credentials hidden>)"

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(method, str) or method not in _PARAMS:
            raise AgentBridgeClientError("unsupported Agent Bridge method")
        if not isinstance(params, dict) or any(key not in _PARAMS[method] for key in params):
            raise AgentBridgeClientError("invalid Agent Bridge parameters")
        alias = params.get("workdir")
        if alias is not None:
            if not isinstance(alias, str) or alias not in self._adapters:
                raise AgentBridgeClientError("workdir is outside the authorized grant")
            return await self._adapters[alias].call(method, params)
        if method in _TASK_OWNED_METHODS:
            if not isinstance(params.get("task_id"), str) or not params["task_id"]:
                raise AgentBridgeClientError("invalid Agent task identifier")
            for adapter in self._adapters.values():
                try:
                    observed = await adapter.call("task.get", {"task_id": params["task_id"]})
                except AgentBridgeRemoteError as exc:
                    if exc.code == "CORE_TASK_NOT_FOUND":
                        continue
                    raise
                if method == "task.get":
                    return observed
                return await adapter.call(method, params)
            raise AgentBridgeRemoteError("CORE_TASK_NOT_FOUND", "Core task is not available")
        if method == "runtime.list":
            # A Core may expose different runtime policies on its signed
            # aliases; the public discovery tool has no workdir parameter.
            runtimes: list[dict[str, Any]] = []
            names: set[str] = set()
            successes = 0
            for adapter in self._adapters.values():
                try:
                    response = await adapter.call(method, params)
                except AgentBridgeRemoteError as exc:
                    if exc.code == "CORE_NOT_AUTHORIZED":
                        continue
                    raise
                entries = response.get("runtimes")
                if not isinstance(entries, list) or any(
                    not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                    for entry in entries
                ):
                    raise AgentBridgeClientError("Bridge v2 returned an invalid runtime catalog")
                successes += 1
                for entry in entries:
                    if entry["name"] not in names:
                        names.add(entry["name"])
                        runtimes.append(entry)
            if not successes:
                raise AgentBridgeClientError("Core Bridge runtime discovery is not authorized")
            return {"runtimes": runtimes}
        if method == "runtime.models":
            for adapter in self._adapters.values():
                try:
                    return await adapter.call(method, params)
                except AgentBridgeRemoteError as exc:
                    if exc.code == "CORE_NOT_AUTHORIZED":
                        continue
                    raise
            raise AgentBridgeClientError("Core Bridge model discovery is not authorized")
        if method == "bridge.ping":
            return await next(iter(self._adapters.values())).call(method, params)
        raise AgentBridgeClientError("Agent Bridge workdir is required")


def _credentials(client: Any) -> tuple[str, ...]:
    values: list[str] = []
    for name in ("_secret", "_bridge_id", "_core_id", "_key_id", "_policy_revision"):
        value = getattr(client, name, None)
        if isinstance(value, bytes):
            values.extend((value.hex(), value.decode("utf-8", errors="ignore")))
        elif isinstance(value, str):
            values.append(value)
    return tuple(value for value in values if value)

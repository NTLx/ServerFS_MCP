"""Adapt Agent-tool RPC calls to the signed macOS Shared Bridge v2 client."""

from __future__ import annotations

import re
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
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_ERROR_MESSAGE = re.compile(r"[A-Za-z0-9 _.,:;()/'-]{1,256}\Z")
_GLOBAL_METHODS = frozenset({"bridge.ping", "runtime.list"})
_AGENT_METHODS = frozenset(
    {
        "task.submit",
        "task.get",
        "task.list",
        "task.events",
        "task.result.read",
        "task.cancel",
        "task.message.send",
        "task.approval.respond",
        "task.question.answer",
    }
)


class DarwinSharedAgentAdapter:
    """Expose one grant-scoped Shared Core as the existing Agent client interface."""

    def __init__(self, client: Any, workdir_alias: str, *, allow_repo_ops: bool = False) -> None:
        if not callable(getattr(client, "call", None)):
            raise ValueError("Shared Bridge client is invalid")
        if not isinstance(workdir_alias, str) or not _ALIAS.fullmatch(workdir_alias):
            raise ValueError("Shared Bridge workdir grant is invalid")
        self._client = client
        self._workdir_alias = workdir_alias
        self._allow_repo_ops = allow_repo_ops

    def __repr__(self) -> str:
        return "DarwinSharedAgentAdapter(<grant credentials hidden>)"

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(method, str) or not method:
            raise AgentBridgeClientError("invalid Agent Bridge method")
        supported = _GLOBAL_METHODS | _AGENT_METHODS | {"runtime.models"}
        if method.startswith("repo."):
            if not self._allow_repo_ops:
                raise AgentBridgeClientError("Repo Ops is unavailable through Agent tools")
            supported |= {"repo.inspect", "repo.action", "repo.commit", "repo.push"}
        if method not in supported:
            raise AgentBridgeClientError("unsupported Agent Bridge method")
        if not isinstance(params, dict):
            raise AgentBridgeClientError("invalid Agent Bridge parameters")

        forwarded = dict(params)
        if "workdir" in forwarded:
            if forwarded["workdir"] != self._workdir_alias:
                raise AgentBridgeClientError("workdir is outside the authorized grant")
            del forwarded["workdir"]
        # Public task lookup / interaction tools take only a task_id; their
        # API does not carry a workdir. The authenticated CoreBridgeClientV2
        # injects this Core's immutable grant alias into EVERY signed v2
        # envelope. An explicitly supplied different workdir is still denied.

        try:
            return await self._client.call(method, forwarded)
        except CoreBridgeRemoteError as exc:
            code, message = self._safe_remote_error(exc.code, exc.message)
            raise AgentBridgeRemoteError(code, message) from None
        except CoreBridgeUnavailable:
            raise AgentBridgeUnavailable("Shared Agent Bridge is unavailable") from None
        except CoreBridgeClientV2Error:
            raise AgentBridgeClientError("Shared Agent Bridge protocol error") from None

    def _safe_remote_error(self, code: object, message: object) -> tuple[str, str]:
        if (
            not isinstance(code, str)
            or not _ERROR_CODE.fullmatch(code)
            or not isinstance(message, str)
            or not _ERROR_MESSAGE.fullmatch(message)
        ):
            return "CORE_BRIDGE_REMOTE_ERROR", "remote request failed"

        credentials = _client_credentials(self._client)
        if any(secret and secret in message for secret in credentials):
            return code, "remote request failed"
        return code, message


def _client_credentials(client: Any) -> tuple[str, ...]:
    """Return only credential representations needed to prevent error reflection."""
    values: list[str] = []
    for attribute in ("_secret", "_bridge_id", "_core_id", "_key_id", "_policy_revision"):
        value = getattr(client, attribute, None)
        if isinstance(value, bytes):
            values.extend((value.hex(), value.decode("utf-8", errors="ignore")))
        elif isinstance(value, str):
            values.append(value)
    return tuple(value for value in values if value)

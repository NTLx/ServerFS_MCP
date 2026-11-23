"""Grant-scoped macOS shared Agent dispatcher for multiple native workdirs.

This is a private Core-facing component: it never provisions grants, owns a
Bridge, or adds Repo Ops methods. Each supplied adapter is already bound to a
different owner-authorized signed v2 workdir alias.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .agent_client import AgentBridgeClientError, AgentBridgeRemoteError
from .darwin_shared_agent_adapter import DarwinSharedAgentAdapter

_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_TASK_LOOKUPS = frozenset(
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


class DarwinSharedMultiAgentAdapter:
    """Route each request through exactly one already authorized Core grant.

    Task-only APIs have no public workdir argument. Resolve them using bounded
    read-only task.get probes against all aliases; mutate only after ownership
    is confirmed for the selected alias. A restart needs no local task cache.
    """

    def __init__(self, clients: Mapping[str, Any]) -> None:
        if not isinstance(clients, Mapping) or not 2 <= len(clients) <= 16:
            raise ValueError("Shared Bridge workdir grants are invalid")
        if any(not isinstance(a, str) or not _ALIAS.fullmatch(a) for a in clients):
            raise ValueError("Shared Bridge workdir grants are invalid")
        self._adapters = {
            alias: DarwinSharedAgentAdapter(client, alias) for alias, client in clients.items()
        }

    def __repr__(self) -> str:
        return "DarwinSharedMultiAgentAdapter(<grant credentials hidden>)"

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(method, str) or not isinstance(params, dict):
            raise AgentBridgeClientError("invalid Agent Bridge request")
        if method.startswith("repo."):
            raise AgentBridgeClientError("Repo Ops is unavailable through Agent tools")
        alias = params.get("workdir")
        if alias is not None:
            if not isinstance(alias, str) or alias not in self._adapters:
                raise AgentBridgeClientError("workdir is outside the authorized grant")
            return await self._adapters[alias].call(method, params)
        if method == "task.submit":
            raise AgentBridgeClientError("Agent task workdir is required")
        if method in _TASK_LOOKUPS:
            task_id = params.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise AgentBridgeClientError("invalid Agent task identifier")
            for adapter in self._adapters.values():
                try:
                    observed = await adapter.call("task.get", {"task_id": task_id})
                except AgentBridgeRemoteError as exc:
                    if exc.code == "CORE_TASK_NOT_FOUND":
                        continue
                    raise
                if method == "task.get":
                    return observed
                # No replay of the original mutation after an uncertain result.
                return await adapter.call(method, params)
            raise AgentBridgeRemoteError("CORE_TASK_NOT_FOUND", "Core task is not available")
        if method == "runtime.list":
            # A shared Core can contain different provider allowlists for
            # different workdirs. Merge only catalog entries returned by its
            # own separately authenticated alias grants.
            discovered: list[dict[str, Any]] = []
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
                    not isinstance(item, dict) or not isinstance(item.get("name"), str)
                    for item in entries
                ):
                    raise AgentBridgeClientError("invalid Bridge runtime catalog")
                successes += 1
                for item in entries:
                    if item["name"] not in names:
                        names.add(item["name"])
                        discovered.append(item)
            if not successes:
                raise AgentBridgeClientError("Shared Core runtime discovery is not authorized")
            return {"runtimes": discovered}
        if method == "runtime.models":
            for adapter in self._adapters.values():
                try:
                    return await adapter.call(method, params)
                except AgentBridgeRemoteError as exc:
                    if exc.code == "CORE_NOT_AUTHORIZED":
                        continue
                    raise
            raise AgentBridgeClientError("Shared Core model discovery is not authorized")
        return await next(iter(self._adapters.values())).call(method, params)

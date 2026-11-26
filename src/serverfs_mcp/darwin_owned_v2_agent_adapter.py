"""Explicit named-owned v2 Agent adapter with narrow per-alias routing."""

from __future__ import annotations

from typing import Any

from .agent_client import AgentBridgeClientError
from .darwin_shared_agent_adapter import DarwinSharedAgentAdapter
from .darwin_shared_multi_agent_adapter import DarwinSharedMultiAgentAdapter


class DarwinOwnedV2AgentAdapter:
    """Route only to this owned Core's explicit per-alias signed grants."""

    def __init__(
        self,
        clients: dict[str, Any],
        *,
        agent_aliases: set[str] | None = None,
        repo_aliases: set[str] | None = None,
    ) -> None:
        if not isinstance(clients, dict) or not 1 <= len(clients) <= 16:
            raise ValueError("owned Bridge workdir grants are invalid")
        agent_aliases = set(clients) if agent_aliases is None else agent_aliases
        repo_aliases = set() if repo_aliases is None else repo_aliases
        if agent_aliases | repo_aliases != set(clients):
            raise ValueError("owned Bridge capability scope is invalid")
        agent_clients = {alias: clients[alias] for alias in clients if alias in agent_aliases}
        self._adapter = None
        if len(agent_clients) == 1:
            alias, client = next(iter(agent_clients.items()))
            self._adapter = DarwinSharedAgentAdapter(client, alias)
        elif agent_clients:
            self._adapter = DarwinSharedMultiAgentAdapter(agent_clients)
        self._repo_adapters = {
            alias: DarwinSharedAgentAdapter(clients[alias], alias, allow_repo_ops=True)
            for alias in repo_aliases
        }

    def __repr__(self) -> str:
        return "DarwinOwnedV2AgentAdapter(<grant credentials hidden>)"

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if (
            method == "task.submit"
            and isinstance(params, dict)
            and (
                "continue_from" in params
                or "continue_from_task_id" in params
                or "idempotency_key" in params
            )
        ):
            raise AgentBridgeClientError(
                "signed-v2 owned Agent does not support continue_from or idempotency_key"
            )
        if isinstance(method, str) and method.startswith("repo."):
            alias = params.get("workdir") if isinstance(params, dict) else None
            if not isinstance(alias, str) or alias not in self._repo_adapters:
                raise AgentBridgeClientError("Repo Ops workdir is not authorized")
            return await self._repo_adapters[alias].call(method, params)
        if self._adapter is None:
            raise AgentBridgeClientError("Agent delegation is unavailable")
        return await self._adapter.call(method, params)

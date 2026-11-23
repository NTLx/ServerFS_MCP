"""macOS shared multi-workdir dispatch contracts (no running Bridge required)."""

from __future__ import annotations

import asyncio

import pytest

from serverfs_mcp.agent_client import AgentBridgeClientError, AgentBridgeRemoteError
from serverfs_mcp.core_bridge_client_v2 import CoreBridgeRemoteError
from serverfs_mcp.darwin_shared_multi_agent_adapter import DarwinSharedMultiAgentAdapter


class SignedClientFixture:
    def __init__(self, alias: str, task_owner: str = "secondary") -> None:
        self._workdir_alias = alias
        self.task_owner = task_owner
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, dict(params)))
        if method == "task.get" and self._workdir_alias != self.task_owner:
            raise CoreBridgeRemoteError("CORE_TASK_NOT_FOUND", "Core task is not available")
        return {"alias": self._workdir_alias, "method": method}


def clients():
    first = SignedClientFixture("primary")
    second = SignedClientFixture("secondary")
    return first, second, DarwinSharedMultiAgentAdapter({"primary": first, "secondary": second})


def test_explicit_task_scope_uses_exact_signed_alias() -> None:
    first, second, adapter = clients()
    result = asyncio.run(
        adapter.call("task.submit", {"workdir": "secondary", "runtime": "claude"})
    )
    assert result == {"alias": "secondary", "method": "task.submit"}
    assert first.calls == []
    assert second.calls == [("task.submit", {"runtime": "claude"})]


def test_task_only_lookup_probes_bounded_aliases_and_resolves_after_restart() -> None:
    first, second, adapter = clients()
    result = asyncio.run(
        adapter.call("task.message.send", {"task_id": "agt_test", "message": "hi"})
    )
    assert result == {"alias": "secondary", "method": "task.message.send"}
    assert first.calls == [("task.get", {"task_id": "agt_test"})]
    assert second.calls == [
        ("task.get", {"task_id": "agt_test"}),
        ("task.message.send", {"task_id": "agt_test", "message": "hi"}),
    ]


def test_task_get_does_not_probe_twice_after_positive_owner() -> None:
    _, second, adapter = clients()
    response = asyncio.run(adapter.call("task.get", {"task_id": "agt_test"}))
    assert response == {"alias": "secondary", "method": "task.get"}
    assert len(second.calls) == 1


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("task.submit", {"runtime": "claude"}),
        ("task.submit", {"workdir": "other", "runtime": "claude"}),
        ("repo.push", {"workdir": "primary"}),
        ("task.cancel", {"workdir": "other", "task_id": "agt_test"}),
    ],
)
def test_missing_or_foreign_scope_and_repo_are_refused(method, params) -> None:
    first, second, adapter = clients()
    with pytest.raises(AgentBridgeClientError):
        asyncio.run(adapter.call(method, params))
    assert not first.calls and not second.calls


def test_unknown_task_get_is_not_disclosed() -> None:
    first = SignedClientFixture("primary", task_owner="absent")
    second = SignedClientFixture("secondary", task_owner="absent")
    adapter = DarwinSharedMultiAgentAdapter({"primary": first, "secondary": second})
    with pytest.raises(AgentBridgeRemoteError) as error:
        asyncio.run(adapter.call("task.get", {"task_id": "not-found"}))
    assert error.value.code == "CORE_TASK_NOT_FOUND"
    assert len(first.calls) == len(second.calls) == 1


def test_invalid_grant_mapping_is_refused_and_repr_is_redacted() -> None:
    with pytest.raises(ValueError):
        DarwinSharedMultiAgentAdapter({"primary": SignedClientFixture("primary")})
    with pytest.raises(ValueError):
        DarwinSharedMultiAgentAdapter(
            {"primary": SignedClientFixture("primary"), "../x": SignedClientFixture("../x")}
        )
    assert "credentials hidden" in repr(clients()[2])


def test_global_runtime_discovery_merges_different_authorized_aliases() -> None:
    class CatalogClient(SignedClientFixture):
        async def call(self, method: str, params: dict) -> dict:
            self.calls.append((method, dict(params)))
            assert method == "runtime.list"
            catalog = {
                "primary": [{"name": "codex", "available": True}],
                "secondary": [
                    {"name": "codex", "available": False},
                    {"name": "qoder", "available": True},
                ],
            }
            return {"runtimes": catalog[self._workdir_alias]}

    first = CatalogClient("primary")
    second = CatalogClient("secondary")
    adapter = DarwinSharedMultiAgentAdapter({"primary": first, "secondary": second})
    output = asyncio.run(adapter.call("runtime.list", {}))
    assert output["runtimes"] == [
        {"name": "codex", "available": True},
        {"name": "qoder", "available": True},
    ]
    assert first.calls == second.calls == [("runtime.list", {})]


def test_runtime_models_can_use_second_authorized_alias() -> None:
    class ModelClient(SignedClientFixture):
        async def call(self, method: str, params: dict) -> dict:
            self.calls.append((method, dict(params)))
            assert method == "runtime.models"
            if self._workdir_alias == "primary":
                raise CoreBridgeRemoteError("CORE_NOT_AUTHORIZED", "not authorized")
            return {"runtime": "qoder", "status": "ok", "models": ["example"]}

    first = ModelClient("primary")
    second = ModelClient("secondary")
    adapter = DarwinSharedMultiAgentAdapter({"primary": first, "secondary": second})
    result = asyncio.run(adapter.call("runtime.models", {"runtime": "qoder"}))
    assert result["runtime"] == "qoder"
    assert first.calls == second.calls == [("runtime.models", {"runtime": "qoder"})]


def test_global_discovery_refuses_malformed_catalog_and_ungranted_models() -> None:
    class NoCatalogClient(SignedClientFixture):
        async def call(self, method: str, params: dict) -> dict:
            if method == "runtime.list":
                return {"runtimes": [{"invalid": True}]}
            raise CoreBridgeRemoteError("CORE_NOT_AUTHORIZED", "not authorized")

    adapter = DarwinSharedMultiAgentAdapter(
        {"primary": NoCatalogClient("primary"), "secondary": NoCatalogClient("secondary")}
    )
    with pytest.raises(AgentBridgeClientError, match="invalid Bridge runtime catalog"):
        asyncio.run(adapter.call("runtime.list", {}))
    with pytest.raises(AgentBridgeClientError, match="not authorized"):
        asyncio.run(adapter.call("runtime.models", {"runtime": "qoder"}))

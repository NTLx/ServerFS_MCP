"""Focused tests for the read-only Shared Agent adapter factory."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from serverfs_mcp.agent_client import AgentBridgeClient
from serverfs_mcp.darwin_shared_agent_adapter import DarwinSharedAgentAdapter
from serverfs_mcp.darwin_shared_agent_factory import prepare_shared_agent_adapter
from serverfs_mcp.darwin_shared_core_bootstrap import SharedCoreBootstrapError
from serverfs_mcp.instance_identity import InstanceIdentity


class AuthenticatedClient:
    _workdir_alias = "Workspace"

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"method": method, "params": params}


def test_factory_uses_only_authenticated_client_grant_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AuthenticatedClient()
    seen: list[tuple[Any, ...]] = []

    async def prepare(*args: Any, **kwargs: Any) -> AuthenticatedClient:
        seen.append((*args, kwargs))
        return client

    monkeypatch.setattr(
        "serverfs_mcp.darwin_shared_agent_factory.prepare_shared_v2_client", prepare
    )

    async def run() -> None:
        adapter = await prepare_shared_agent_adapter(
            InstanceIdentity("core-a", "shared", "shared-bridge"),
            Path("/trusted/core.toml"),
            Path("/private/bridge.sock"),
            home=Path("/private/home"),
            timeout_seconds=3,
        )
        assert isinstance(adapter, DarwinSharedAgentAdapter)
        assert await adapter.call("task.get", {"task_id": "t1", "workdir": "Workspace"}) == {
            "method": "task.get",
            "params": {"task_id": "t1"},
        }

    asyncio.run(run())
    assert len(seen) == 1
    assert seen[0][0] == InstanceIdentity("core-a", "shared", "shared-bridge")
    assert seen[0][3] == {
        "home": Path("/private/home"),
        "timeout_seconds": 3,
    }


@pytest.mark.parametrize(
    "identity",
    [
        InstanceIdentity("core-a", "owned", "core-a"),
        InstanceIdentity("core-a", "disabled"),
        InstanceIdentity("default", "shared", "shared-bridge"),
    ],
)
def test_factory_rejects_owned_disabled_and_default(
    identity: InstanceIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid identity must fail before bootstrap")

    monkeypatch.setattr(
        "serverfs_mcp.darwin_shared_agent_factory.prepare_shared_v2_client", forbidden
    )

    async def run() -> None:
        with pytest.raises(SharedCoreBootstrapError, match="could not be verified"):
            await prepare_shared_agent_adapter(
                identity, Path("/trusted/core.toml"), Path("/private/bridge.sock")
            )

    asyncio.run(run())


def test_factory_preserves_sanitized_failure_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise SharedCoreBootstrapError("shared Bridge client could not be verified")

    monkeypatch.setattr("serverfs_mcp.darwin_shared_agent_factory.prepare_shared_v2_client", fail)

    async def run_failure() -> None:
        with pytest.raises(SharedCoreBootstrapError) as caught:
            await prepare_shared_agent_adapter(
                InstanceIdentity("core-a", "shared", "shared-bridge"),
                Path("/trusted/core.toml"),
                Path("/private/bridge.sock"),
            )
        assert str(caught.value) == "shared Bridge client could not be verified"

    asyncio.run(run_failure())

    async def cancel(*args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr("serverfs_mcp.darwin_shared_agent_factory.prepare_shared_v2_client", cancel)

    async def run_cancelled() -> None:
        with pytest.raises(asyncio.CancelledError):
            await prepare_shared_agent_adapter(
                InstanceIdentity("core-a", "shared", "shared-bridge"),
                Path("/trusted/core.toml"),
                Path("/private/bridge.sock"),
            )

    asyncio.run(run_cancelled())


def test_v1_client_contract_is_unchanged() -> None:
    from serverfs_mcp.agent_client import PROTOCOL_VERSION

    assert PROTOCOL_VERSION == 1
    assert AgentBridgeClient is not None

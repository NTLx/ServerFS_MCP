"""``bridge.ping`` — the internal supervisor liveness RPC (v0.13.1).

The watchdog's only question is "does this Bridge still answer its event loop?".
The method is therefore dispatched inside the protocol server, never in
``BridgeService``, and must perform no runtime, provider, discovery or network
work. These tests are platform-neutral: they drive ``_dispatch`` directly, so
the contract holds on the Unix-socket and Named-Pipe transports alike.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.protocol import BridgeProtocolServer
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore


def make_server(tmp_path: Path) -> BridgeProtocolServer:
    repo = tmp_path / "repo"
    repo.mkdir()
    service = BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=PolicyRegistry(
            [
                WorkdirAgentPolicy(
                    slot=None if sys.platform == "win32" else 1,
                    alias="repo",
                    host_path=repo,
                    mode=AgentMode.WORKSPACE_WRITE,
                    runtimes=frozenset({"fake"}),
                    read_only=False,
                )
            ]
        ),
        adapters={"fake": FakeAdapter()},
        lease_manager=LeaseManager(tmp_path / "locks"),
    )
    kwargs: dict = {"service": service}
    if sys.platform != "win32":
        # Only the POSIX branch requires a socket path at construction time; neither
        # platform binds anything because start() is never called here.
        kwargs["socket_path"] = tmp_path / "run" / "bridge.sock"
    return BridgeProtocolServer(**kwargs)


@pytest.mark.asyncio
async def test_bridge_ping_returns_deterministic_ok(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    assert await server._dispatch("bridge.ping", {}) == {"status": "ok"}


@pytest.mark.asyncio
async def test_bridge_ping_rejects_non_empty_params(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    with pytest.raises(BridgeError) as raised:
        await server._dispatch("bridge.ping", {"unexpected": True})
    assert raised.value.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_bridge_ping_never_reaches_the_service(tmp_path: Path, monkeypatch) -> None:
    server = make_server(tmp_path)

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bridge.ping must not call BridgeService")

    monkeypatch.setattr(server.service, "list_runtimes", _refuse)
    monkeypatch.setattr(server.service, "list_models", _refuse)
    monkeypatch.setattr(server.service, "submit_task", _refuse)
    assert await server._dispatch("bridge.ping", {}) == {"status": "ok"}

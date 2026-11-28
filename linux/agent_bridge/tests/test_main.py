from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from platform_contract import linux_only
from serverfs_agent_bridge import main as main_module
from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.main import _serve

pytestmark = linux_only("the Bridge entry point starts the Unix-socket RPC server")


@pytest.mark.asyncio
async def test_serve_shutdown_event_closes_and_removes_socket(tmp_path: Path) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    config_path = tmp_path / "config.json"
    socket_path = tmp_path / "run" / "bridge.sock"
    config_path.write_text(
        json.dumps(
            {
                "socket_path": str(socket_path),
                "state_dir": str(tmp_path / "state"),
                "lock_dir": str(tmp_path / "locks"),
                "allowed_peer_uid": None,
                "allowed_peer_gid": None,
                "enable_fake_runtime": True,
                "codex": {"enabled": False},
                "claude": {"enabled": False},
                "workdirs": [
                    {
                        "slot": 1,
                        "alias": "scratch",
                        "host_path": str(workdir),
                        "read_only": False,
                        "agent_mode": "workspace-write",
                        "agent_runtimes": ["fake"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config = BridgeConfig.load(config_path)
    shutdown = asyncio.Event()
    task = asyncio.create_task(_serve(config, shutdown_event=shutdown))

    for _ in range(200):
        if socket_path.exists():
            break
        await asyncio.sleep(0.01)
    assert socket_path.exists()

    shutdown.set()
    await asyncio.wait_for(task, timeout=5)

    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_serve_registers_each_configured_acp_profile(tmp_path: Path, monkeypatch) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    socket_path = tmp_path / "run" / "bridge.sock"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "socket_path": str(socket_path),
                "state_dir": str(tmp_path / "state"),
                "lock_dir": str(tmp_path / "locks"),
                "allowed_peer_uid": None,
                "allowed_peer_gid": None,
                "codex": {"enabled": False},
                "claude": {"enabled": False},
                "acp_profiles": [
                    {"id": "one", "command": sys.executable},
                    {"id": "two", "command": sys.executable},
                ],
                "workdirs": [
                    {
                        "slot": 1,
                        "alias": "scratch",
                        "host_path": str(workdir),
                        "read_only": False,
                        "agent_mode": "workspace-write",
                        "agent_runtimes": ["acp-one", "acp-two"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config = BridgeConfig.load(config_path)
    registered: list[str] = []

    class CapturingAdapter(FakeAdapter):
        def __init__(
            self,
            profile,
            *,
            runtime_proxy=None,
            load_capabilities=None,
            save_capabilities=None,
        ):
            super().__init__(runtime_proxy=runtime_proxy)
            self._name = profile.runtime_id
            registered.append(self._name)

        @property
        def name(self):
            return self._name

    monkeypatch.setattr(main_module.runtime_adapters, "AcpAdapter", CapturingAdapter)
    shutdown = asyncio.Event()
    task = asyncio.create_task(_serve(config, shutdown_event=shutdown))
    for _ in range(200):
        if socket_path.exists():
            break
        await asyncio.sleep(0.01)
    shutdown.set()
    await asyncio.wait_for(task, timeout=5)
    assert registered == ["acp-one", "acp-two"]

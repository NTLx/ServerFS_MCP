from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.main import _serve


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

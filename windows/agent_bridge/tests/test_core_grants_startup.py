from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from serverfs_agent_bridge import config as config_module
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
from serverfs_agent_bridge.core_grants_private import provision_private_core_grants
from serverfs_agent_bridge.main import _serve
from serverfs_agent_bridge.store import TaskStore


def _write_config(tmp_path: Path, *, protocol_mode: str | None = None) -> Path:
    workdir = tmp_path / "workdir"
    workdir.mkdir(exist_ok=True)
    data = {
        "socket_path": str(tmp_path / "run" / "bridge.sock"),
        "state_dir": str(tmp_path / "state"),
        "lock_dir": str(tmp_path / "locks"),
        "enable_fake_runtime": True,
        "workdirs": [
            {
                "slot": 1,
                "alias": "repo",
                "host_path": str(workdir),
                "read_only": True,
                "agent_mode": "review",
                "agent_runtimes": ["fake"],
            }
        ],
    }
    if protocol_mode is not None:
        data["protocol_mode"] = protocol_mode
    if protocol_mode == "v2" and sys.platform == "linux":
        data["lease_key"] = "alias"
        data["workdirs"][0].pop("slot")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _v2_config(tmp_path: Path) -> BridgeConfig:
    path = _write_config(tmp_path, protocol_mode="v2")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["bridge_id"] = "bridge-test"
    path.write_text(json.dumps(data), encoding="utf-8")
    return BridgeConfig.load(path)


def test_v1_protocol_defaults_are_unchanged(tmp_path: Path) -> None:
    config = BridgeConfig.load(_write_config(tmp_path))
    assert config.protocol_mode == "v1"
    assert config.bridge_id is None


@pytest.mark.parametrize(
    ("mode", "bridge_id"),
    [("v3", None), ("v2", None), ("v1", "bridge-test")],
)
def test_protocol_configuration_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    bridge_id: str | None,
) -> None:
    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    path = _write_config(tmp_path, protocol_mode=mode)
    data = json.loads(path.read_text(encoding="utf-8"))
    if bridge_id is not None:
        data["bridge_id"] = bridge_id
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        BridgeConfig.load(path)


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform not in {"darwin", "linux"}, reason="requires native POSIX Bridge v2"
)
async def test_v2_missing_private_grants_fails_before_listener(tmp_path: Path) -> None:
    config = _v2_config(tmp_path)
    with pytest.raises(ValueError, match="Core grants file"):
        await _serve(config, shutdown_event=asyncio.Event())
    assert not config.socket_path.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="requires native Darwin AF_UNIX")
async def test_v2_owner_grants_start_and_reject_v1_request(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="sfs-", dir="/tmp") as runtime_root:
        runtime_dir = Path(runtime_root)
        runtime_dir.chmod(0o700)
        config = replace(_v2_config(tmp_path), socket_path=runtime_dir / "b.sock")
        store = TaskStore(config.state_dir)
        policy = config.policies.get("repo")
        provision_private_core_grants(
            store=store,
            bridge_id="bridge-test",
            policies=config.policies,
            document={
                "schema_version": 1,
                "bridge_id": "bridge-test",
                "grants": [
                    {
                        "core_id": "core-a",
                        "key_id": "key-a",
                        "secret_hex": bytes(range(32)).hex(),
                        "methods": ["bridge.ping"],
                        "workdirs": [
                            {
                                "alias": "repo",
                                "policy_sha256": core_workdir_policy_digest(policy),
                            }
                        ],
                    }
                ],
            },
        )
        shutdown = asyncio.Event()
        task = asyncio.create_task(_serve(config, shutdown_event=shutdown))
        try:
            for _ in range(200):
                if config.socket_path.exists():
                    break
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            assert config.socket_path.exists()
            reader, writer = await asyncio.open_unix_connection(str(config.socket_path))
            writer.write(b'{"protocol_version":1,"request_id":"legacy"}\n')
            await writer.drain()
            response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            assert response["ok"] is False
        finally:
            shutdown.set()
            if not task.done():
                await asyncio.wait_for(task, timeout=5)
            else:
                await task
        assert not config.socket_path.exists()

"""Linux source-only acceptance for two signed Cores sharing one Bridge v2 socket."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from platform_contract import linux_only
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest, sign_core_request
from serverfs_agent_bridge.core_grants_private import provision_private_core_grants
from serverfs_agent_bridge.main import _serve
from serverfs_agent_bridge.store import TaskStore

pytestmark = linux_only("Linux AF_UNIX two-Core Bridge v2 isolation")

BRIDGE_ID = "linux-v0250"
CORES = (
    ("core-alpha", "key-alpha", bytes(range(32)), "AlphaRepo"),
    ("core-beta", "key-beta", bytes(range(32, 64)), "BetaRepo"),
)


def _load_client_classes():
    source = Path(__file__).resolve().parents[2] / "src/serverfs_mcp/core_bridge_client_v2.py"
    spec = importlib.util.spec_from_file_location("linux_v0250_two_core_client", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CoreBridgeClientV2, module.CoreBridgeRemoteError


def _config(tmp_path: Path) -> BridgeConfig:
    workdirs = []
    for _core, _key, _secret, alias in CORES:
        root = tmp_path / alias
        root.mkdir()
        workdirs.append(
            {
                "alias": alias,
                "host_path": str(root),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": ["fake"],
            }
        )
    config_path = tmp_path / "bridge.json"
    config_path.write_text(
        json.dumps(
            {
                "socket_path": str(tmp_path / "run" / "bridge.sock"),
                "state_dir": str(tmp_path / "state"),
                "lock_dir": str(tmp_path / "locks"),
                "allowed_peer_uid": os.getuid(),
                "enable_fake_runtime": True,
                "protocol_mode": "v2",
                "lease_key": "alias",
                "bridge_id": BRIDGE_ID,
                "workdirs": workdirs,
            }
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    return BridgeConfig.load(config_path)


def _install_grants(config: BridgeConfig, cores=CORES) -> None:
    methods = ["bridge.ping", "task.submit", "task.get"]
    provision_private_core_grants(
        store=TaskStore(config.state_dir),
        bridge_id=BRIDGE_ID,
        policies=config.policies,
        document={
            "schema_version": 1,
            "bridge_id": BRIDGE_ID,
            "grants": [
                {
                    "core_id": core_id,
                    "key_id": key_id,
                    "secret_hex": secret.hex(),
                    "methods": methods,
                    "workdirs": [
                        {
                            "alias": alias,
                            "policy_sha256": core_workdir_policy_digest(config.policies.get(alias)),
                        }
                    ],
                }
                for core_id, key_id, secret, alias in cores
            ],
        },
    )


async def _start(config: BridgeConfig) -> tuple[asyncio.Event, asyncio.Task[None]]:
    stop = asyncio.Event()
    serving = asyncio.create_task(_serve(config, shutdown_event=stop))
    for _ in range(300):
        if serving.done():
            await serving
        if config.socket_path.exists():
            return stop, serving
        await asyncio.sleep(0.01)
    raise AssertionError("Bridge v2 socket did not become ready")


async def _stop(stop: asyncio.Event, serving: asyncio.Task[None]) -> None:
    stop.set()
    await asyncio.wait_for(serving, timeout=5)


def _signed(
    config: BridgeConfig,
    *,
    core: tuple[str, str, bytes, str],
    method: str,
    nonce: str,
    params: dict[str, Any] | None = None,
    workdir: str | None = None,
    policy_revision: str | None = None,
) -> dict[str, Any]:
    core_id, key_id, secret, alias = core
    unsigned = {
        "protocol_version": 2,
        "bridge_id": BRIDGE_ID,
        "core_id": core_id,
        "key_id": key_id,
        "request_id": f"req-{nonce[:12]}",
        "method": method,
        "params": {"workdir": workdir or alias, **(params or {})},
        "policy_revision": policy_revision
        or core_workdir_policy_digest(config.policies.get(workdir or alias)),
        "issued_at": int(time.time()),
        "nonce": nonce,
    }
    return {**unsigned, "signature": sign_core_request(unsigned, secret)}


async def _raw_rpc(path: Path, request: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(path=str(path))
    try:
        writer.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()


def test_linux_v0250_two_core_shared_bridge_isolation_and_replay(tmp_path: Path) -> None:
    client_type, remote_error = _load_client_classes()
    config = _config(tmp_path)
    _install_grants(config)

    def client(core: tuple[str, str, bytes, str]):
        core_id, key_id, secret, alias = core
        return client_type(
            config.socket_path,
            bridge_id=BRIDGE_ID,
            core_id=core_id,
            key_id=key_id,
            secret=secret,
            workdir_alias=alias,
            policy_revision=core_workdir_policy_digest(config.policies.get(alias)),
            timeout_seconds=3,
        )

    async def exercise() -> None:
        stop, serving = await _start(config)
        try:
            clients = [client(core) for core in CORES]
            task_ids = []
            for core_client in clients:
                assert await core_client.call("bridge.ping", {}) == {"status": "ok"}
                submitted = await core_client.call(
                    "task.submit",
                    {
                        "runtime": "fake",
                        "path": ".",
                        "profile": "review",
                        "prompt": "complete:isolated result",
                    },
                )
                task_ids.append(submitted["task_id"])

            for core_client, task_id in zip(clients, task_ids, strict=True):
                task = await core_client.call("task.get", {"task_id": task_id})
                assert task["task_id"] == task_id

            for core_client, foreign_task_id in (
                (clients[0], task_ids[1]),
                (clients[1], task_ids[0]),
            ):
                with pytest.raises(remote_error) as denied:
                    await core_client.call("task.get", {"task_id": foreign_task_id})
                assert denied.value.code == "CORE_TASK_NOT_FOUND"

            # Repo Ops is deliberately omitted from both grants; authorization must stop
            # the signed call before dispatch can reach Git or any repository operation.
            denied_method = await _raw_rpc(
                config.socket_path,
                _signed(
                    config,
                    core=CORES[0],
                    method="repo.inspect",
                    nonce="a" * 32,
                    params={"view": "status"},
                ),
            )
            assert denied_method["error"]["code"] == "CORE_NOT_AUTHORIZED"

            # A signed request bound to the other Core's alias/policy is outside this grant.
            wrong_alias = await _raw_rpc(
                config.socket_path,
                _signed(
                    config,
                    core=CORES[0],
                    method="bridge.ping",
                    nonce="b" * 32,
                    workdir=CORES[1][3],
                ),
            )
            assert wrong_alias["error"]["code"] == "CORE_NOT_AUTHORIZED"

            replay = _signed(config, core=CORES[0], method="bridge.ping", nonce="c" * 32)
            assert (await _raw_rpc(config.socket_path, replay))["ok"] is True
        finally:
            await _stop(stop, serving)

        # The nonce claim is durable in the existing Bridge store and survives restart.
        stop_again, serving_again = await _start(config)
        try:
            response = await _raw_rpc(config.socket_path, replay)
            assert response["error"]["code"] == "CORE_NOT_AUTHORIZED"
        finally:
            await _stop(stop_again, serving_again)

    asyncio.run(exercise())

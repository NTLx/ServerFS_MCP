"""Linux AF_UNIX acceptance for the opt-in authenticated Bridge v2 protocol."""

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

pytestmark = linux_only("Linux AF_UNIX SO_PEERCRED Bridge v2 acceptance")

BRIDGE_ID = "linux-v0250"
CORE_ID = "core-alpha"
KEY_ID = "key-alpha"
SECRET = bytes(range(32))


def _load_client_class():
    source = Path(__file__).resolve().parents[2] / "src/serverfs_mcp/core_bridge_client_v2.py"
    spec = importlib.util.spec_from_file_location("linux_v0250_core_bridge_client_v2", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CoreBridgeClientV2, module.CoreBridgeRemoteError


def _config(tmp_path: Path, *, allowed_peer_uid: int | None = None) -> BridgeConfig:
    workdir = tmp_path / "repo"
    workdir.mkdir(exist_ok=True)
    config_path = tmp_path / "bridge.json"
    document = {
        "socket_path": str(tmp_path / "run" / "bridge.sock"),
        "state_dir": str(tmp_path / "state"),
        "lock_dir": str(tmp_path / "locks"),
        "allowed_peer_uid": os.getuid() if allowed_peer_uid is None else allowed_peer_uid,
        "enable_fake_runtime": True,
        "protocol_mode": "v2",
        "lease_key": "alias",
        "bridge_id": BRIDGE_ID,
        "workdirs": [
            {
                "alias": "Repo",
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": ["fake"],
            }
        ],
    }
    config_path.write_text(json.dumps(document), encoding="utf-8")
    config_path.chmod(0o600)
    return BridgeConfig.load(config_path)


def _digest(config: BridgeConfig) -> str:
    return core_workdir_policy_digest(config.policies.get("Repo"))


def _install_grant(config: BridgeConfig, *, grants: list[dict[str, Any]] | None = None) -> None:
    store = TaskStore(config.state_dir)
    policy_digest = _digest(config)
    entries = (
        grants if grants is not None else [{"core_id": CORE_ID, "key_id": KEY_ID, "secret": SECRET}]
    )
    provision_private_core_grants(
        store=store,
        bridge_id=BRIDGE_ID,
        policies=config.policies,
        document={
            "schema_version": 1,
            "bridge_id": BRIDGE_ID,
            "grants": [
                {
                    "core_id": item["core_id"],
                    "key_id": item["key_id"],
                    "secret_hex": item["secret"].hex(),
                    "methods": ["bridge.ping"],
                    "workdirs": [{"alias": "Repo", "policy_sha256": policy_digest}],
                }
                for item in entries
            ],
        },
    )


async def _start(config: BridgeConfig) -> tuple[asyncio.Event, asyncio.Task[None]]:
    stop = asyncio.Event()
    task = asyncio.create_task(_serve(config, shutdown_event=stop))
    for _ in range(300):
        if task.done():
            await task
        if config.socket_path.exists():
            return stop, task
        await asyncio.sleep(0.01)
    raise AssertionError("Bridge v2 socket did not become ready")


async def _stop(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop.set()
    await asyncio.wait_for(task, timeout=5)


def _signed(
    config: BridgeConfig,
    *,
    nonce: str,
    bridge_id: str = BRIDGE_ID,
    core_id: str = CORE_ID,
    key_id: str = KEY_ID,
    secret: bytes = SECRET,
    policy_revision: str | None = None,
) -> dict[str, Any]:
    unsigned = {
        "protocol_version": 2,
        "bridge_id": bridge_id,
        "core_id": core_id,
        "key_id": key_id,
        "request_id": f"req-{nonce[:12]}",
        "method": "bridge.ping",
        "params": {"workdir": "Repo"},
        "policy_revision": policy_revision or _digest(config),
        "issued_at": int(time.time()),
        "nonce": nonce,
    }
    return {**unsigned, "signature": sign_core_request(unsigned, secret)}


async def _raw_rpc(path: Path, request: dict[str, Any] | bytes) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(path=str(path))
    try:
        raw = (
            request
            if isinstance(request, bytes)
            else (json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
        )
        writer.write(raw)
        await writer.drain()
        response = await reader.readline()
        return json.loads(response) if response else {}
    finally:
        writer.close()
        await writer.wait_closed()


def test_linux_v2_client_ping_authentication_and_restart_replay(tmp_path: Path) -> None:
    client_type, remote_error = _load_client_class()
    config = _config(tmp_path)
    _install_grant(config)

    async def exercise() -> None:
        stop, serving = await _start(config)
        try:
            client = client_type(
                config.socket_path,
                bridge_id=BRIDGE_ID,
                core_id=CORE_ID,
                key_id=KEY_ID,
                secret=SECRET,
                workdir_alias="Repo",
                policy_revision=_digest(config),
                timeout_seconds=3,
            )
            assert await client.call("bridge.ping", {}) == {"status": "ok"}

            # Independently signed invalid identities and stale policy fail closed.
            wrong_bridge = await _raw_rpc(
                config.socket_path, _signed(config, nonce="1" * 32, bridge_id="other-bridge")
            )
            assert wrong_bridge["error"]["code"] == "CORE_NOT_AUTHORIZED"
            wrong_core = await _raw_rpc(
                config.socket_path, _signed(config, nonce="2" * 32, core_id="core-missing")
            )
            assert wrong_core["error"]["code"] == "CORE_NOT_AUTHORIZED"
            stale_policy = await _raw_rpc(
                config.socket_path,
                _signed(config, nonce="3" * 32, policy_revision="0" * 64),
            )
            assert stale_policy["error"]["code"] == "CORE_NOT_AUTHORIZED"

            replay = _signed(config, nonce="4" * 32)
            assert (await _raw_rpc(config.socket_path, replay))["ok"] is True
            assert (await _raw_rpc(config.socket_path, replay))["error"]["code"] == (
                "CORE_NOT_AUTHORIZED"
            )
            assert (await _raw_rpc(config.socket_path, b"{invalid-json}\n"))["error"][
                "code"
            ] == "INVALID_REQUEST"
            oversized = await _raw_rpc(config.socket_path, b"{" + b" " * 1_048_576 + b"}\n")
            assert oversized.get("ok") is False
            assert isinstance(oversized.get("error", {}).get("code"), str)

            wrong_client = client_type(
                config.socket_path,
                bridge_id=BRIDGE_ID,
                core_id=CORE_ID,
                key_id=KEY_ID,
                secret=b"x" * 32,
                workdir_alias="Repo",
                policy_revision=_digest(config),
                timeout_seconds=3,
            )
            with pytest.raises(remote_error) as denied:
                await wrong_client.call("bridge.ping", {})
            assert denied.value.code == "CORE_NOT_AUTHORIZED"
        finally:
            await _stop(stop, serving)

        # Durable TaskStore nonce claims remain active after the Bridge process restarts.
        stop_again, serving_again = await _start(config)
        try:
            response = await _raw_rpc(config.socket_path, replay)
            assert response["error"]["code"] == "CORE_NOT_AUTHORIZED"
        finally:
            await _stop(stop_again, serving_again)

    asyncio.run(exercise())


def test_linux_v2_missing_and_revoked_grants_fail_closed(tmp_path: Path) -> None:
    from serverfs_agent_bridge.core_grants_private import load_private_core_grants

    config = _config(tmp_path)
    store = TaskStore(config.state_dir)
    with pytest.raises(ValueError, match="invalid or not private"):
        load_private_core_grants(store=store, bridge_id=BRIDGE_ID, policies=config.policies)
    _install_grant(config, grants=[])
    verifier = load_private_core_grants(store=store, bridge_id=BRIDGE_ID, policies=config.policies)
    with pytest.raises(Exception) as denied:
        verifier.authorize(_signed(config, nonce="5" * 32))
    assert getattr(denied.value, "code", None) == "CORE_NOT_AUTHORIZED"


def test_linux_v2_peer_uid_denial(tmp_path: Path) -> None:
    config = _config(tmp_path, allowed_peer_uid=os.getuid() + 1)
    _install_grant(config)

    async def exercise() -> None:
        stop, serving = await _start(config)
        try:
            response = await _raw_rpc(config.socket_path, _signed(config, nonce="6" * 32))
            assert response["error"]["code"] == "PEER_NOT_AUTHORIZED"
        finally:
            await _stop(stop, serving)

    asyncio.run(exercise())

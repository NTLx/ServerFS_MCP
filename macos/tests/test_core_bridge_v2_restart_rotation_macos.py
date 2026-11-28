"""Isolated Darwin Bridge v2 grant rotation, revocation and durable replay E2E.

This test owns only a disposable /tmp fixture. It does not install services,
use production workdirs, or change existing operator grants.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin AF_UNIX only")

_BRIDGE_SRC = Path(__file__).resolve().parents[1] / "agent_bridge" / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))

from serverfs_agent_bridge.config import BridgeConfig  # noqa: E402
from serverfs_agent_bridge.core_grants import (  # noqa: E402
    core_workdir_policy_digest,
    sign_core_request,
)
from serverfs_agent_bridge.core_grants_private import provision_private_core_grants  # noqa: E402
from serverfs_agent_bridge.main import _serve  # noqa: E402
from serverfs_agent_bridge.store import TaskStore  # noqa: E402

from serverfs_mcp.core_bridge_client_v2 import (  # noqa: E402
    CoreBridgeClientV2,
    CoreBridgeRemoteError,
)

_BRIDGE = "rotation-bridge"
_OLD = bytes(range(32))
_NEW = bytes(range(32, 64))
_PEER = b"z" * 32


def _grant(core: str, key: str, secret: bytes, digest: str, *, submit: bool) -> dict:
    methods = ["bridge.ping", "task.get"]
    if submit:
        methods.append("task.submit")
    return {
        "core_id": core,
        "key_id": key,
        "secret_hex": secret.hex(),
        "methods": methods,
        "workdirs": [{"alias": "Repo", "policy_sha256": digest}],
    }


def _client(path: Path, digest: str, core: str, key: str, secret: bytes) -> CoreBridgeClientV2:
    return CoreBridgeClientV2(
        path,
        bridge_id=_BRIDGE,
        core_id=core,
        key_id=key,
        secret=secret,
        workdir_alias="Repo",
        policy_revision=digest,
        timeout_seconds=3,
    )


async def _read_response(path: Path, request: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()


async def _start(config: BridgeConfig) -> tuple[asyncio.Event, asyncio.Task]:
    stopped = asyncio.Event()
    task = asyncio.create_task(_serve(config, shutdown_event=stopped))
    for _ in range(300):
        if task.done():
            await task
        if config.socket_path.exists():
            return stopped, task
        await asyncio.sleep(0.01)
    stopped.set()
    await asyncio.wait_for(task, timeout=5)
    raise AssertionError("isolated Bridge v2 did not start")


async def _stop(config: BridgeConfig, shutdown: asyncio.Event, task: asyncio.Task) -> None:
    shutdown.set()
    await asyncio.wait_for(task, timeout=5)
    assert not config.socket_path.exists()


def test_explicit_grant_rotation_and_revoke_require_bridge_restart() -> None:
    with tempfile.TemporaryDirectory(prefix="sfs-rotate-", dir="/tmp") as directory:
        root = Path(directory)
        root.chmod(0o700)
        workdir = root / "repo"
        workdir.mkdir(mode=0o700)
        config_path = root / "bridge.json"
        config_path.write_text(
            json.dumps(
                {
                    "socket_path": str(root / "b.sock"),
                    "protocol_mode": "v2",
                    "bridge_id": _BRIDGE,
                    "state_dir": str(root / "state"),
                    "lock_dir": str(root / "locks"),
                    "allowed_peer_uid": os.getuid(),
                    "enable_fake_runtime": True,
                    "workdirs": [
                        {
                            "slot": 1,
                            "alias": "Repo",
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
        config_path.chmod(0o600)
        config = BridgeConfig.load(config_path)
        digest = core_workdir_policy_digest(config.policies.get("Repo"))
        store = TaskStore(config.state_dir)

        def publish(*grants: dict) -> None:
            provision_private_core_grants(
                store=store,
                bridge_id=_BRIDGE,
                policies=config.policies,
                document={
                    "schema_version": 1,
                    "bridge_id": _BRIDGE,
                    "grants": list(grants),
                },
            )

        old = _client(config.socket_path, digest, "core-a", "old", _OLD)
        rotated = _client(config.socket_path, digest, "core-a", "rotated", _NEW)
        peer = _client(config.socket_path, digest, "core-b", "peer", _PEER)

        async def denied(client: CoreBridgeClientV2) -> None:
            with pytest.raises(CoreBridgeRemoteError) as error:
                await client.call("bridge.ping", {})
            assert error.value.code == "CORE_NOT_AUTHORIZED"

        async def exercise() -> None:
            publish(
                _grant("core-a", "old", _OLD, digest, submit=True),
                _grant("core-b", "peer", _PEER, digest, submit=False),
            )
            shutdown, task = await _start(config)
            try:
                assert await old.call("bridge.ping", {}) == {"status": "ok"}
                assert await peer.call("bridge.ping", {}) == {"status": "ok"}
                submitted = await old.call(
                    "task.submit",
                    {"runtime": "fake", "path": ".", "profile": "review", "prompt": "synthetic"},
                )
                task_id = submitted["task_id"]
                assert (await old.call("task.get", {"task_id": task_id}))["task_id"] == task_id
                with pytest.raises(CoreBridgeRemoteError) as other_owner:
                    await peer.call("task.get", {"task_id": task_id})
                assert other_owner.value.code == "CORE_TASK_NOT_FOUND"
                await denied(rotated)

                publish(
                    _grant("core-a", "rotated", _NEW, digest, submit=True),
                    _grant("core-b", "peer", _PEER, digest, submit=False),
                )
                # Atomic persistent grants do not hot reload into a running Bridge.
                assert await old.call("bridge.ping", {}) == {"status": "ok"}
                await denied(rotated)
            finally:
                await _stop(config, shutdown, task)

            shutdown, task = await _start(config)
            try:
                await denied(old)
                assert await rotated.call("bridge.ping", {}) == {"status": "ok"}
                assert await peer.call("bridge.ping", {}) == {"status": "ok"}

                # Prove durable nonces survive an owner-controlled restart.
                unsigned = {
                    "protocol_version": 2,
                    "bridge_id": _BRIDGE,
                    "core_id": "core-a",
                    "key_id": "rotated",
                    "request_id": "fixed-nonce-before-restart",
                    "method": "bridge.ping",
                    "params": {"workdir": "Repo"},
                    "policy_revision": digest,
                    "issued_at": int(time.time()),
                    "nonce": "a" * 32,
                }
                signed = {**unsigned, "signature": sign_core_request(unsigned, _NEW)}
                assert (await _read_response(config.socket_path, signed))["ok"] is True

                publish(_grant("core-a", "rotated", _NEW, digest, submit=True))
                # Revoking peer is effective only after a Bridge owner restart.
                assert await peer.call("bridge.ping", {}) == {"status": "ok"}
            finally:
                await _stop(config, shutdown, task)

            shutdown, task = await _start(config)
            try:
                assert await rotated.call("bridge.ping", {}) == {"status": "ok"}
                await denied(peer)
                replay = await _read_response(config.socket_path, signed)
                assert replay["ok"] is False
                assert replay["error"]["code"] == "CORE_NOT_AUTHORIZED"
                await denied(old)
            finally:
                await _stop(config, shutdown, task)

        asyncio.run(exercise())

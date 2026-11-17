"""Native startup acceptance for the private, v2-only macOS Core Bridge."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires native Darwin AF_UNIX")

# Root pytest uses the ServerFS environment; the Bridge is a separate source package.
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

_SECRET_A = bytes(range(32))
_SECRET_B = bytes(range(32, 64))
_BRIDGE_ID = "startup-v2"


async def _raw_call(path: Path, request: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()


def _signed_v2(digest: str, *, nonce: str, version: int = 2) -> dict[str, Any]:
    unsigned = {
        "protocol_version": version,
        "bridge_id": _BRIDGE_ID,
        "core_id": "core-a",
        "key_id": "key-a",
        "request_id": f"startup-{nonce[:8]}",
        "method": "bridge.ping",
        "params": {"workdir": "Repo"},
        "policy_revision": digest,
        "issued_at": int(time.time()),
        "nonce": nonce,
    }
    return {**unsigned, "signature": sign_core_request(unsigned, _SECRET_A)}


def test_v2_native_startup_loads_private_grants_and_serves_only_v2() -> None:
    with tempfile.TemporaryDirectory(prefix="sfs-v2-", dir="/tmp") as root:
        root_path = Path(root)
        os.chmod(root_path, 0o700)
        socket_path = root_path / "b.sock"
        workdir = root_path / "repo"
        workdir.mkdir(mode=0o700)
        config_path = root_path / "bridge.json"
        state_dir = root_path / "state"
        lock_dir = root_path / "locks"
        document = {
            "socket_path": str(socket_path),
            "protocol_mode": "v2",
            "bridge_id": _BRIDGE_ID,
            "state_dir": str(state_dir),
            "lock_dir": str(lock_dir),
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
        config_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(config_path, 0o600)
        config = BridgeConfig.load(config_path)
        assert config.protocol_mode == "v2"
        digest = core_workdir_policy_digest(config.policies.get("Repo"))
        store = TaskStore(state_dir)
        provision_private_core_grants(
            store=store,
            bridge_id=_BRIDGE_ID,
            policies=config.policies,
            document={
                "schema_version": 1,
                "bridge_id": _BRIDGE_ID,
                "grants": [
                    {
                        "core_id": "core-a",
                        "key_id": "key-a",
                        "secret_hex": _SECRET_A.hex(),
                        "methods": ["bridge.ping", "task.submit", "task.get"],
                        "workdirs": [{"alias": "Repo", "policy_sha256": digest}],
                    },
                    {
                        "core_id": "core-b",
                        "key_id": "key-b",
                        "secret_hex": _SECRET_B.hex(),
                        "methods": ["task.get"],
                        "workdirs": [{"alias": "Repo", "policy_sha256": digest}],
                    },
                ],
            },
        )

        async def exercise() -> None:
            stop = asyncio.Event()
            serving = asyncio.create_task(_serve(config, shutdown_event=stop))
            try:
                for _ in range(200):
                    if serving.done():
                        await serving
                    if socket_path.exists():
                        break
                    await asyncio.sleep(0.01)
                assert socket_path.exists(), "_serve did not create the configured v2 socket"
                assert len(os.fsencode(socket_path)) < 104
                client_a = CoreBridgeClientV2(
                    socket_path,
                    bridge_id=_BRIDGE_ID,
                    core_id="core-a",
                    key_id="key-a",
                    secret=_SECRET_A,
                    workdir_alias="Repo",
                    policy_revision=digest,
                    timeout_seconds=3,
                )
                client_b = CoreBridgeClientV2(
                    socket_path,
                    bridge_id=_BRIDGE_ID,
                    core_id="core-b",
                    key_id="key-b",
                    secret=_SECRET_B,
                    workdir_alias="Repo",
                    policy_revision=digest,
                    timeout_seconds=3,
                )
                submitted = await client_a.call(
                    "task.submit",
                    {"runtime": "fake", "path": ".", "profile": "review", "prompt": "synthetic"},
                )
                task_id = submitted["task_id"]
                assert (await client_a.call("task.get", {"task_id": task_id}))["task_id"] == task_id
                with pytest.raises(CoreBridgeRemoteError) as denied:
                    await client_b.call("task.get", {"task_id": task_id})
                assert denied.value.code == "CORE_TASK_NOT_FOUND"

                with pytest.raises(CoreBridgeRemoteError) as wrong_key:
                    await CoreBridgeClientV2(
                        socket_path,
                        bridge_id=_BRIDGE_ID,
                        core_id="core-a",
                        key_id="key-a",
                        secret=b"x" * 32,
                        workdir_alias="Repo",
                        policy_revision=digest,
                        timeout_seconds=3,
                    ).call("task.get", {"task_id": task_id})
                assert wrong_key.value.code == "CORE_NOT_AUTHORIZED"
                with pytest.raises(CoreBridgeRemoteError) as wrong_policy:
                    await CoreBridgeClientV2(
                        socket_path,
                        bridge_id=_BRIDGE_ID,
                        core_id="core-a",
                        key_id="key-a",
                        secret=_SECRET_A,
                        workdir_alias="Repo",
                        policy_revision="0" * 64,
                        timeout_seconds=3,
                    ).call("task.get", {"task_id": task_id})
                assert wrong_policy.value.code == "CORE_NOT_AUTHORIZED"

                replay = _signed_v2(digest, nonce="a" * 32)
                assert (await _raw_call(socket_path, replay))["ok"] is True
                repeated = await _raw_call(socket_path, replay)
                assert repeated["ok"] is False
                assert repeated["error"]["code"] == "CORE_NOT_AUTHORIZED"
                downgrade = await _raw_call(
                    socket_path, _signed_v2(digest, nonce="b" * 32, version=1)
                )
                assert downgrade["ok"] is False
                assert downgrade["error"]["code"] == "CORE_NOT_AUTHORIZED"
            finally:
                stop.set()
                await asyncio.wait_for(serving, timeout=5)
            assert not socket_path.exists()

        asyncio.run(exercise())

        # Loading a config with no protocol_mode preserves the historical v1 default.
        v1_document = {
            key: value
            for key, value in document.items()
            if key not in {"protocol_mode", "bridge_id"}
        }
        v1_path = root_path / "bridge-v1.json"
        v1_path.write_text(json.dumps(v1_document), encoding="utf-8")
        os.chmod(v1_path, 0o600)
        assert BridgeConfig.load(v1_path).protocol_mode == "v1"

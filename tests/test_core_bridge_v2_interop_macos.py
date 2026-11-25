"""Native Darwin interoperability between CoreBridgeClientV2 and Bridge v2."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires native Darwin AF_UNIX")

# Root tests run in the ServerFS environment; the Bridge is a separate source package.
_BRIDGE_SRC = Path(__file__).resolve().parents[1] / "agent_bridge" / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))

from serverfs_agent_bridge.adapters import FakeAdapter  # noqa: E402
from serverfs_agent_bridge.core_grants import (  # noqa: E402
    CoreGrant,
    CoreGrantVerifier,
    core_workdir_policy_digest,
    sign_core_request,
)
from serverfs_agent_bridge.leases import LeaseManager  # noqa: E402
from serverfs_agent_bridge.models import AgentMode  # noqa: E402
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy  # noqa: E402
from serverfs_agent_bridge.protocol import BridgeProtocolServer  # noqa: E402
from serverfs_agent_bridge.service import BridgeService  # noqa: E402
from serverfs_agent_bridge.store import TaskStore  # noqa: E402

from serverfs_mcp.core_bridge_client_v2 import (  # noqa: E402
    CoreBridgeClientV2,
    CoreBridgeRemoteError,
)

SECRET_A = bytes(range(32))
SECRET_B = bytes(range(32, 64))


def _server(tmp_path: Path, state: Path, socket_path: Path) -> tuple[BridgeProtocolServer, str]:
    workdir = tmp_path / "repo"
    workdir.mkdir(exist_ok=True)
    policy = WorkdirAgentPolicy(
        slot=1,
        alias="Repo",
        host_path=workdir,
        mode=AgentMode.WORKSPACE_WRITE,
        runtimes=frozenset({"fake"}),
        read_only=False,
    )
    digest = core_workdir_policy_digest(policy)
    store = TaskStore(state)
    service = BridgeService(
        store=store,
        policies=PolicyRegistry([policy]),
        adapters={"fake": FakeAdapter()},
        lease_manager=LeaseManager(tmp_path / "locks"),
    )
    grants = (
        CoreGrant(
            "core-a",
            "key-a",
            SECRET_A,
            (("Repo", digest),),
            frozenset({"bridge.ping", "task.submit", "task.get", "task.list"}),
        ),
        CoreGrant("core-b", "key-b", SECRET_B, (("Repo", digest),), frozenset({"task.get"})),
    )
    server = BridgeProtocolServer(
        service=service,
        socket_path=socket_path,
        allowed_peer_uid=__import__("os").getuid(),
        v2_only=True,
        core_grant_verifier=CoreGrantVerifier(
            "bridge-v2", grants, nonce_claim=store.claim_core_nonce
        ),
    )
    return server, digest


def _client(
    socket_path: Path,
    digest: str,
    *,
    core: str = "core-a",
    key: str = "key-a",
    secret: bytes = SECRET_A,
) -> CoreBridgeClientV2:
    return CoreBridgeClientV2(
        socket_path,
        bridge_id="bridge-v2",
        core_id=core,
        key_id=key,
        secret=secret,
        workdir_alias="Repo",
        policy_revision=digest,
        timeout_seconds=3,
    )


async def _raw_call(socket_path: Path, envelope: dict[str, Any]) -> dict[str, Any]:
    import asyncio
    import json

    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(json.dumps(envelope, separators=(",", ":")).encode() + b"\n")
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


def _signed(
    digest: str, *, nonce: str, method: str = "bridge.ping", version: int = 2
) -> dict[str, Any]:
    unsigned = {
        "protocol_version": version,
        "bridge_id": "bridge-v2",
        "core_id": "core-a",
        "key_id": "key-a",
        "request_id": f"interop-{nonce[:8]}",
        "method": method,
        "params": {"workdir": "Repo"},
        "policy_revision": digest,
        "issued_at": int(time.time()),
        "nonce": nonce,
    }
    return {**unsigned, "signature": sign_core_request(unsigned, SECRET_A)}


def test_real_core_clients_interoperate_with_v2_only_darwin_bridge(tmp_path: Path) -> None:
    async def exercise(socket_path: Path) -> None:
        server, digest = _server(tmp_path, tmp_path / "state", socket_path)
        await server.start()
        try:
            client_a = _client(socket_path, digest)
            client_b = _client(socket_path, digest, core="core-b", key="key-b", secret=SECRET_B)

            submitted = await client_a.call(
                "task.submit",
                {
                    "runtime": "fake",
                    "path": ".",
                    "profile": "review",
                    "prompt": "synthetic",
                },
            )
            task_id = submitted["task_id"]
            assert (await client_a.call("task.get", {"task_id": task_id}))["task_id"] == task_id
            from serverfs_mcp.darwin_shared_agent_adapter import DarwinSharedAgentAdapter

            shared_agent = DarwinSharedAgentAdapter(client_a, "Repo")
            page = await shared_agent.call(
                "task.list", {"workdir": "Repo", "status": "all", "limit": 20}
            )
            assert [task["task_id"] for task in page["tasks"]] == [task_id]
            assert page["tasks"][0]["correlation_id"] is None
            assert page["next_cursor"] is None
            with pytest.raises(CoreBridgeRemoteError) as cross_owner:
                await client_b.call("task.get", {"task_id": task_id})
            assert cross_owner.value.code == "CORE_TASK_NOT_FOUND"
            with pytest.raises(CoreBridgeRemoteError) as no_discovery_grant:
                await client_b.call("task.list", {"status": "all"})
            assert no_discovery_grant.value.code == "CORE_NOT_AUTHORIZED"

            with pytest.raises(CoreBridgeRemoteError) as wrong_key:
                await _client(socket_path, digest, secret=b"x" * 32).call(
                    "task.get", {"task_id": task_id}
                )
            assert wrong_key.value.code == "CORE_NOT_AUTHORIZED"
            with pytest.raises(CoreBridgeRemoteError) as wrong_policy:
                await _client(socket_path, "0" * 64).call("task.get", {"task_id": task_id})
            assert wrong_policy.value.code == "CORE_NOT_AUTHORIZED"

            replay = _signed(digest, nonce="a" * 32)
            assert (await _raw_call(socket_path, replay))["ok"] is True
            duplicate = await _raw_call(socket_path, replay)
            assert duplicate["ok"] is False
            assert duplicate["error"]["code"] == "CORE_NOT_AUTHORIZED"
            downgrade = await _raw_call(socket_path, _signed(digest, nonce="b" * 32, version=1))
            assert downgrade["ok"] is False
            assert downgrade["error"]["code"] == "CORE_NOT_AUTHORIZED"
        finally:
            await server.close()
        assert not socket_path.exists()

    with tempfile.TemporaryDirectory(prefix="sfs-", dir="/tmp") as socket_root:
        socket_dir = Path(socket_root)
        os.chmod(socket_dir, 0o700)
        socket_path = socket_dir / "b.sock"
        assert len(os.fsencode(str(socket_path))) < 104
        asyncio.run(exercise(socket_path))

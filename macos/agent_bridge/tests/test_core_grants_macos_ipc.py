"""Native Darwin AF_UNIX acceptance for the isolated per-Core Bridge v2 path."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.core_grants import (
    CoreGrant,
    CoreGrantVerifier,
    core_workdir_policy_digest,
    sign_core_request,
)
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.protocol import BridgeProtocolServer
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires native Darwin AF_UNIX")

SECRET_A = bytes(range(32))
SECRET_B = bytes(range(32, 64))


def _service(tmp_path: Path, store: TaskStore) -> tuple[BridgeService, WorkdirAgentPolicy]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    policy = WorkdirAgentPolicy(
        slot=1,
        alias="Repo",
        host_path=repo,
        mode=AgentMode.WORKSPACE_WRITE,
        runtimes=frozenset({"fake"}),
        read_only=False,
    )
    return (
        BridgeService(
            store=store,
            policies=PolicyRegistry([policy]),
            adapters={"fake": FakeAdapter()},
            lease_manager=LeaseManager(tmp_path / "locks"),
        ),
        policy,
    )


def _v2_server(tmp_path: Path, store: TaskStore, socket_path: Path) -> BridgeProtocolServer:
    service, policy = _service(tmp_path, store)
    digest = core_workdir_policy_digest(policy)
    grants = (
        CoreGrant(
            "core-a",
            "key-a",
            SECRET_A,
            (("Repo", digest),),
            frozenset({"bridge.ping", "task.get", "task.submit"}),
        ),
        CoreGrant("core-b", "key-b", SECRET_B, (("Repo", digest),), frozenset({"task.get"})),
    )
    return BridgeProtocolServer(
        service=service,
        socket_path=socket_path,
        allowed_peer_uid=os.getuid(),
        v2_only=True,
        core_grant_verifier=CoreGrantVerifier(
            "bridge-v2", grants, nonce_claim=store.claim_core_nonce
        ),
    )


def _signed(
    server: BridgeProtocolServer,
    method: str,
    *,
    core_id: str = "core-a",
    key_id: str = "key-a",
    secret: bytes = SECRET_A,
    nonce: str,
    params: dict[str, Any] | None = None,
    protocol_version: int = 2,
) -> dict[str, Any]:
    unsigned = {
        "protocol_version": protocol_version,
        "bridge_id": "bridge-v2",
        "core_id": core_id,
        "key_id": key_id,
        "request_id": f"req-{nonce[:8]}",
        "method": method,
        "params": {"workdir": "Repo", **(params or {})},
        "policy_revision": core_workdir_policy_digest(server.service.policies.get("Repo")),
        "issued_at": int(time.time()),
        "nonce": nonce,
    }
    return {**unsigned, "signature": sign_core_request(unsigned, secret)}


@pytest.mark.asyncio
async def test_legacy_v1_owned_bridge_rejects_task_list_without_downgrade(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "bridge-state")
    service, _ = _service(tmp_path, store)
    server = BridgeProtocolServer(service=service, socket_path=tmp_path / "legacy.sock")
    response = await server._dispatch_raw(
        json.dumps(
            {
                "protocol_version": 1,
                "request_id": "legacy-list",
                "method": "task.list",
                "params": {"workdir": "Repo", "status": "active"},
            }
        ).encode()
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "CORE_NOT_AUTHORIZED"


async def _rpc(path: Path, request: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


@pytest.mark.asyncio
async def test_two_core_v2_real_darwin_unix_ipc_and_v1_isolation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    socket_root = Path(tempfile.mkdtemp(prefix="sfs-", dir="/tmp"))
    socket_path = socket_root / "v2.sock"
    store = TaskStore(state)
    server = _v2_server(tmp_path, store, socket_path)
    await server.start()
    try:
        info = socket_path.lstat()
        assert stat.S_ISSOCK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600

        # Darwin's actual getpeereid measurement on the connected client must match this process.
        peer_reader, peer_writer = await asyncio.open_unix_connection(str(socket_path))
        del peer_reader
        server._check_peer(peer_writer)
        peer_writer.close()
        await peer_writer.wait_closed()

        submit = await _rpc(
            socket_path,
            _signed(
                server,
                "task.submit",
                nonce="1" * 32,
                params={
                    "runtime": "fake",
                    "path": ".",
                    "profile": "review",
                    "prompt": "synthetic acceptance",
                },
            ),
        )
        assert submit["ok"] is True
        task_id = submit["result"]["task_id"]
        owner_read = await _rpc(
            socket_path,
            _signed(server, "task.get", nonce="2" * 32, params={"task_id": task_id}),
        )
        assert owner_read["ok"] is True
        assert owner_read["result"]["task_id"] == task_id

        cross_core = await _rpc(
            socket_path,
            _signed(
                server,
                "task.get",
                core_id="core-b",
                key_id="key-b",
                secret=SECRET_B,
                nonce="3" * 32,
                params={"task_id": task_id},
            ),
        )
        assert cross_core["ok"] is False
        assert cross_core["error"]["code"] == "CORE_TASK_NOT_FOUND"

        tampered = _signed(server, "bridge.ping", nonce="4" * 32)
        tampered["signature"] = "0" * 64
        response = await _rpc(socket_path, tampered)
        assert response["ok"] is False
        assert response["error"]["code"] == "CORE_NOT_AUTHORIZED"

        replay = _signed(server, "bridge.ping", nonce="5" * 32)
        assert (await _rpc(socket_path, replay))["ok"] is True
        await server.close()

        # Durable nonce claims survive reopening the SQLite store and constructing a new verifier.
        reopened = TaskStore(state)
        reopened_server = _v2_server(tmp_path, reopened, socket_path)
        await reopened_server.start()
        server = reopened_server
        replay_response = await _rpc(socket_path, replay)
        assert replay_response["ok"] is False
        assert replay_response["error"]["code"] == "CORE_NOT_AUTHORIZED"

        downgrade = _signed(server, "bridge.ping", nonce="6" * 32, protocol_version=1)
        downgrade_response = await _rpc(socket_path, downgrade)
        assert downgrade_response["ok"] is False
        assert downgrade_response["error"]["code"] == "CORE_NOT_AUTHORIZED"
    finally:
        await server.close()
    assert not socket_path.exists()

    # The ordinary v1 mode remains independent and does not require Core grants.
    v1_path = socket_root / "v1.sock"
    service, _policy = _service(tmp_path, TaskStore(tmp_path / "v1-state"))
    v1_server = BridgeProtocolServer(service=service, socket_path=v1_path)
    await v1_server.start()
    try:
        legacy = await _rpc(
            v1_path,
            {
                "protocol_version": 1,
                "request_id": "legacy-ping",
                "method": "runtime.list",
                "params": {},
            },
        )
        assert legacy["ok"] is True
        assert legacy["request_id"] == "legacy-ping"
    finally:
        await v1_server.close()
    assert not v1_path.exists()
    socket_root.rmdir()

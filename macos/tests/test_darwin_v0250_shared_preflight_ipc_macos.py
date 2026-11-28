"""Combined Shared Bridge source preflight and real v2 IPC acceptance on Darwin."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires native Darwin AF_UNIX")

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

from serverfs_mcp.core_bridge_client_v2 import CoreBridgeClientV2  # noqa: E402
from serverfs_mcp.darwin_instance_shared_binding import (  # noqa: E402
    DarwinSharedBindingError,
    SharedBridgeBinding,
    SharedBridgeGrant,
    preflight_shared_core_binding,
)
from serverfs_mcp.darwin_shared_bridge_readiness import (  # noqa: E402
    probe_shared_v2_bridge,
)
from serverfs_mcp.instance_identity import InstanceIdentity  # noqa: E402

SECRET_A = bytes(range(32))
SECRET_B = bytes(range(32, 64))


def test_shared_preflight_and_readiness_against_live_v2_bridge(tmp_path: Path) -> None:
    async def exercise(socket_path: Path, workdir: Path) -> None:
        policy = WorkdirAgentPolicy(
            slot=1,
            alias="Repo",
            host_path=workdir,
            mode=AgentMode.WORKSPACE_WRITE,
            runtimes=frozenset({"fake"}),
            read_only=False,
        )
        # Shared grants use the Bridge owner's canonical effective-policy digest.
        digest = core_workdir_policy_digest(policy)
        store = TaskStore(tmp_path / "bridge-state")
        service = BridgeService(
            store=store,
            policies=PolicyRegistry([policy]),
            adapters={"fake": FakeAdapter()},
            lease_manager=LeaseManager(tmp_path / "bridge-locks"),
        )
        grants = (
            CoreGrant("core-a", "key-a", SECRET_A, (("Repo", digest),), frozenset({"bridge.ping"})),
            CoreGrant(
                "core-b", "key-b", SECRET_B, (("Other", digest),), frozenset({"bridge.ping"})
            ),
        )
        server = BridgeProtocolServer(
            service=service,
            socket_path=socket_path,
            allowed_peer_uid=os.getuid(),
            v2_only=True,
            core_grant_verifier=CoreGrantVerifier(
                "shared-bridge", grants, nonce_claim=store.claim_core_nonce
            ),
        )
        await server.start()
        try:
            core_config = tmp_path / "core.toml"
            core_config.write_text(
                "[agent]\nenabled = true\n"
                "[agent.codex]\nenabled = true\n"
                "[[workdirs]]\nalias = 'Repo'\n"
                f"path = '{workdir}'\nread_only = false\n"
                "agent_mode = 'workspace-write'\nagent_runtimes = ['codex']\n",
                encoding="utf-8",
            )
            os.chmod(core_config, 0o600)
            bridge_config = tmp_path / "bridge.json"
            bridge_config.write_text(
                json.dumps(
                    {
                        "protocol_mode": "v2",
                        "bridge_id": "shared-bridge",
                        "socket_path": str(socket_path),
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(bridge_config, 0o600)

            identity_a = InstanceIdentity("core-a", "shared", "shared-bridge")
            binding_a = SharedBridgeBinding(
                bridge_config,
                socket_path,
                SharedBridgeGrant(
                    "shared-bridge", "core-a", "key-a", SECRET_A, "Repo", workdir, digest
                ),
                digest,
            )
            assert preflight_shared_core_binding(identity_a, core_config, binding_a) == socket_path

            def client(core_id: str, key_id: str, secret: bytes, alias: str = "Repo"):
                return CoreBridgeClientV2(
                    socket_path,
                    bridge_id="shared-bridge",
                    core_id=core_id,
                    key_id=key_id,
                    secret=secret,
                    workdir_alias=alias,
                    policy_revision=digest,
                    timeout_seconds=3,
                )

            client_a = client("core-a", "key-a", SECRET_A)
            assert await probe_shared_v2_bridge(client_a, socket_path, require_private_parent=True)

            # Core B has a distinct key and an Other-only grant; Repo is denied.
            client_b = client("core-b", "key-b", SECRET_B)
            assert not await probe_shared_v2_bridge(
                client_b, socket_path, require_private_parent=True
            )
            binding_b = SharedBridgeBinding(
                bridge_config,
                socket_path,
                SharedBridgeGrant(
                    "shared-bridge", "core-b", "key-b", SECRET_B, "Other", workdir, digest
                ),
                digest,
            )
            with pytest.raises(DarwinSharedBindingError):
                preflight_shared_core_binding(
                    InstanceIdentity("core-b", "shared", "shared-bridge"),
                    core_config,
                    binding_b,
                )

            # A wrong signing key is denied for Core A as well.
            assert not await probe_shared_v2_bridge(
                client("core-a", "key-a", b"x" * 32),
                socket_path,
                require_private_parent=True,
            )

            unsigned: dict[str, Any] = {
                "protocol_version": 1,
                "bridge_id": "shared-bridge",
                "core_id": "core-a",
                "key_id": "key-a",
                "request_id": "v1-downgrade",
                "method": "bridge.ping",
                "params": {"workdir": "Repo"},
                "policy_revision": digest,
                "issued_at": int(time.time()),
                "nonce": "a" * 32,
            }
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write(
                json.dumps(
                    {**unsigned, "signature": sign_core_request(unsigned, SECRET_A)},
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            await writer.drain()
            response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            assert response["ok"] is False
            assert response["error"]["code"] == "CORE_NOT_AUTHORIZED"

            # Negative Core checks leave the same Bridge serving Core A.
            assert await probe_shared_v2_bridge(client_a, socket_path, require_private_parent=True)
        finally:
            await server.close()
        assert not socket_path.exists()

    socket_dir = Path("/private/tmp") / f"sfs-{uuid.uuid4().hex[:12]}"
    socket_dir.mkdir(mode=0o700)
    os.chmod(socket_dir, 0o700)
    try:
        socket_path = socket_dir / "b.sock"
        assert len(os.fsencode(str(socket_path))) < 104
        workdir = tmp_path / "repo"
        workdir.mkdir(mode=0o700)
        asyncio.run(exercise(socket_path, workdir))
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)

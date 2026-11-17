"""End-to-end source-only Shared binding loader and authenticated Darwin IPC."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

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
    preflight_shared_core_binding,
)
from serverfs_mcp.darwin_shared_binding_private import load_shared_core_binding  # noqa: E402
from serverfs_mcp.darwin_shared_bridge_readiness import probe_shared_v2_bridge  # noqa: E402
from serverfs_mcp.instance_identity import InstanceIdentity  # noqa: E402

SECRET_A = bytes(range(32))
SECRET_B = bytes(range(32, 64))


def test_private_binding_loader_preflight_and_real_v2_ipc(tmp_path: Path) -> None:
    async def exercise(root: Path, socket_path: Path, core_home: Path, workdir: Path) -> None:
        policy = WorkdirAgentPolicy(
            slot=1,
            alias="Repo",
            host_path=workdir,
            mode=AgentMode.WORKSPACE_WRITE,
            runtimes=frozenset({"fake"}),
            read_only=False,
        )
        digest = core_workdir_policy_digest(policy)
        store = TaskStore(root / "bridge-state")
        service = BridgeService(
            store=store,
            policies=PolicyRegistry([policy]),
            adapters={"fake": FakeAdapter()},
            lease_manager=LeaseManager(root / "bridge-locks"),
        )
        verifier = CoreGrantVerifier(
            "shared-bridge",
            (
                CoreGrant(
                    "core-a", "key-a", SECRET_A, (("Repo", digest),), frozenset({"bridge.ping"})
                ),
                CoreGrant(
                    "core-b", "key-b", SECRET_B, (("Other", digest),), frozenset({"bridge.ping"})
                ),
            ),
            nonce_claim=store.claim_core_nonce,
        )
        server = BridgeProtocolServer(
            service=service,
            socket_path=socket_path,
            allowed_peer_uid=os.getuid(),
            v2_only=True,
            core_grant_verifier=verifier,
        )
        await server.start()
        try:
            core_config = root / "core.toml"
            core_config.write_text(
                "[agent]\nenabled = true\n[agent.codex]\nenabled = true\n"
                "[[workdirs]]\nalias = 'Repo'\n"
                f"path = '{workdir}'\nread_only = false\n"
                "agent_mode = 'workspace-write'\nagent_runtimes = ['codex']\n",
                encoding="utf-8",
            )
            core_config.chmod(0o600)
            bridge_config = root / "bridge.json"
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
            bridge_config.chmod(0o600)
            binding_path = core_home / "shared-bridge-binding.json"
            binding_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "bridge_id": "shared-bridge",
                        "core_id": "core-a",
                        "key_id": "key-a",
                        "secret_hex": SECRET_A.hex(),
                        "workdir_alias": "Repo",
                        "workdir_path": str(workdir),
                        "policy_revision": digest,
                        "bridge_config_path": str(bridge_config),
                        "socket_path": str(socket_path),
                    }
                ),
                encoding="utf-8",
            )
            binding_path.chmod(0o600)

            identity_a = InstanceIdentity("core-a", "shared", "shared-bridge")
            binding_a = load_shared_core_binding(
                identity=identity_a,
                trusted_core_home=core_home,
                binding_path=binding_path,
                expected_socket_path=socket_path,
            )
            assert preflight_shared_core_binding(identity_a, core_config, binding_a) == socket_path
            client_a = CoreBridgeClientV2(
                binding_a.socket_path,
                bridge_id=binding_a.grant.bridge_id,
                core_id=binding_a.grant.core_id,
                key_id=binding_a.grant.key_id,
                secret=binding_a.grant.secret,
                workdir_alias=binding_a.grant.workdir_alias,
                policy_revision=binding_a.grant.policy_revision,
                timeout_seconds=3,
            )
            assert await probe_shared_v2_bridge(client_a, socket_path, require_private_parent=True)

            client_b = CoreBridgeClientV2(
                socket_path,
                bridge_id="shared-bridge",
                core_id="core-b",
                key_id="key-b",
                secret=SECRET_B,
                workdir_alias="Repo",
                policy_revision=digest,
                timeout_seconds=3,
            )
            assert not await probe_shared_v2_bridge(
                client_b, socket_path, require_private_parent=True
            )
            with pytest.raises(DarwinSharedBindingError):
                preflight_shared_core_binding(
                    InstanceIdentity("core-b", "shared", "shared-bridge"), core_config, binding_a
                )

            verifier.replace_grants(())
            assert not await probe_shared_v2_bridge(
                client_a, socket_path, require_private_parent=True
            )
        finally:
            await server.close()
        assert not socket_path.exists()

    root = Path("/private/tmp") / f"sfs-{uuid.uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    core_home = root / "core"
    core_home.mkdir(mode=0o700)
    workdir = root / "repo"
    workdir.mkdir(mode=0o700)
    socket_path = root / "b.sock"
    try:
        assert len(os.fsencode(str(socket_path))) < 104
        asyncio.run(exercise(root, socket_path, core_home, workdir))
    finally:
        shutil.rmtree(root, ignore_errors=True)

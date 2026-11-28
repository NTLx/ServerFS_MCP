"""Native macOS integration acceptance for the Shared Agent adapter factory."""

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

from serverfs_mcp.agent_client import AgentBridgeClientError  # noqa: E402
from serverfs_mcp.core_bridge_client_v2 import (  # noqa: E402
    CoreBridgeClientV2,
    CoreBridgeRemoteError,
)
from serverfs_mcp.darwin_instances import instance_service_home  # noqa: E402
from serverfs_mcp.darwin_shared_agent_factory import prepare_shared_agent_adapter  # noqa: E402
from serverfs_mcp.darwin_shared_core_bootstrap import SharedCoreBootstrapError  # noqa: E402
from serverfs_mcp.instance_identity import InstanceIdentity  # noqa: E402

_SECRET_A = bytes(range(32))
_SECRET_B = bytes(range(32, 64))
_BRIDGE_ID = "shared-bridge"


@pytest.mark.parametrize("multi", [False, True], ids=["single", "multi"])
def test_shared_agent_factory_submits_and_reads_only_owned_workdir_tasks(multi: bool) -> None:
    root = Path("/private/tmp") / f"sfsa-{uuid.uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    home = root / "home"
    home.mkdir(mode=0o700)
    workspace = root / "Workspace"
    workspace.mkdir(mode=0o700)
    secondary = root / "Secondary"
    if multi:
        secondary.mkdir(mode=0o700)
    socket_path = root / "bridge.sock"
    state_dir = root / "state"
    identity_a = InstanceIdentity("core-a", "shared", _BRIDGE_ID)
    identity_b = InstanceIdentity("core-b", "shared", _BRIDGE_ID)
    core_home_a = instance_service_home(identity_a, home=home)
    core_home_b = instance_service_home(identity_b, home=home)
    core_home_a.mkdir(parents=True, mode=0o700)
    core_home_b.mkdir(parents=True, mode=0o700)
    config_path = root / "core.toml"
    config_path.write_text(
        "[agent]\nenabled = true\n[agent.codex]\nenabled = true\n"
        "[[workdirs]]\nalias = 'Workspace'\n"
        f"path = '{workspace}'\nread_only = false\n"
        "agent_mode = 'workspace-write'\nagent_runtimes = ['codex']\n"
        + (
            "[[workdirs]]\nalias = 'Secondary'\n"
            f"path = '{secondary}'\nread_only = false\n"
            "agent_mode = 'workspace-write'\nagent_runtimes = ['codex']\n"
            if multi
            else ""
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    bridge_config = root / "bridge.json"
    bridge_config.write_text(
        json.dumps(
            {"protocol_mode": "v2", "bridge_id": _BRIDGE_ID, "socket_path": str(socket_path)}
        ),
        encoding="utf-8",
    )
    bridge_config.chmod(0o600)

    async def exercise() -> None:
        policy = WorkdirAgentPolicy(
            slot=1,
            alias="Workspace",
            host_path=workspace,
            mode=AgentMode.WORKSPACE_WRITE,
            runtimes=frozenset({"fake"}),
            read_only=False,
        )
        digest = core_workdir_policy_digest(policy)
        policies = [policy]
        owner_grants = [("Workspace", digest)]
        if multi:
            second_policy = WorkdirAgentPolicy(
                slot=2,
                alias="Secondary",
                host_path=secondary,
                mode=AgentMode.WORKSPACE_WRITE,
                runtimes=frozenset({"fake"}),
                read_only=False,
            )
            policies.append(second_policy)
            owner_grants.append(("Secondary", core_workdir_policy_digest(second_policy)))
        store = TaskStore(state_dir)
        service = BridgeService(
            store=store,
            policies=PolicyRegistry(policies),
            adapters={"fake": FakeAdapter()},
            lease_manager=LeaseManager(root / "locks"),
        )
        verifier = CoreGrantVerifier(
            _BRIDGE_ID,
            (
                CoreGrant(
                    "core-a",
                    "key-a",
                    _SECRET_A,
                    tuple(owner_grants),
                    frozenset({"bridge.ping", "task.submit", "task.get"}),
                ),
                CoreGrant(
                    "core-b",
                    "key-b",
                    _SECRET_B,
                    (("Workspace", digest),),
                    frozenset({"task.get"}),
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

        def write_binding(core_home: Path, core_id: str, key_id: str, secret: bytes) -> None:
            binding = {
                "schema_version": 1,
                "bridge_id": _BRIDGE_ID,
                "core_id": core_id,
                "key_id": key_id,
                "secret_hex": secret.hex(),
                "workdir_alias": "Workspace",
                "workdir_path": str(workspace),
                "policy_revision": digest,
                "bridge_config_path": str(bridge_config),
                "socket_path": str(socket_path),
            }
            if multi and core_id == "core-a":
                binding["schema_version"] = 2
                for field in ("workdir_alias", "workdir_path", "policy_revision"):
                    del binding[field]
                binding["workdirs"] = [
                    {"alias": name, "path": str(target), "policy_revision": revision}
                    for (name, revision), target in zip(
                        owner_grants, (workspace, secondary), strict=True
                    )
                ]
            path = core_home / "shared-bridge-binding.json"
            path.write_text(json.dumps(binding), encoding="utf-8")
            path.chmod(0o600)

        try:
            write_binding(core_home_a, "core-a", "key-a", _SECRET_A)
            write_binding(core_home_b, "core-b", "key-b", _SECRET_B)
            await server.start()
            adapter = await prepare_shared_agent_adapter(
                identity_a, config_path, socket_path, home=home, timeout_seconds=3
            )
            assert len(os.fsencode(socket_path)) < 104
            submitted = await adapter.call(
                "task.submit",
                {
                    "workdir": "Workspace",
                    "runtime": "fake",
                    "path": ".",
                    "profile": "review",
                    "prompt": "isolated synthetic task",
                },
            )
            task_id = submitted["task_id"]
            assert store.get_task(task_id).task_id == task_id
            store.require_core_task_owner(task_id, core_id="core-a", workdir_alias="Workspace")
            fetched = await adapter.call("task.get", {"task_id": task_id, "workdir": "Workspace"})
            assert fetched["task_id"] == task_id

            with pytest.raises(AgentBridgeClientError, match="outside the authorized grant"):
                await adapter.call("task.get", {"task_id": task_id, "workdir": "Other"})
            with pytest.raises(AgentBridgeClientError, match="Repo Ops"):
                await adapter.call("repo.inspect", {"workdir": "Workspace", "view": "status"})
            assert await adapter.call("bridge.ping", {}) == {"status": "ok"}

            client_b = CoreBridgeClientV2(
                socket_path,
                bridge_id=_BRIDGE_ID,
                core_id="core-b",
                key_id="key-b",
                secret=_SECRET_B,
                workdir_alias="Workspace",
                policy_revision=digest,
                timeout_seconds=3,
            )
            with pytest.raises(CoreBridgeRemoteError) as denied:
                await client_b.call("task.get", {"task_id": task_id})
            assert denied.value.code == "CORE_TASK_NOT_FOUND"
            if multi:
                second = await adapter.call(
                    "task.submit",
                    {
                        "workdir": "Secondary",
                        "runtime": "fake",
                        "path": ".",
                        "profile": "review",
                        "prompt": "isolated second task",
                    },
                )
                second_id = second["task_id"]
                assert second_id != task_id
                store.require_core_task_owner(
                    second_id, core_id="core-a", workdir_alias="Secondary"
                )
                for owned_id in (task_id, second_id):
                    assert (await adapter.call("task.get", {"task_id": owned_id}))[
                        "task_id"
                    ] == owned_id
                with pytest.raises(CoreBridgeRemoteError) as foreign:
                    await client_b.call("task.get", {"task_id": second_id})
                assert foreign.value.code == "CORE_TASK_NOT_FOUND"
                with pytest.raises(AgentBridgeClientError, match="Repo Ops"):
                    await adapter.call("repo.inspect", {"workdir": "Secondary", "view": "status"})
                # A stale second owner grant must refuse the entire new bootstrap,
                # even though the first alias and the already-running owner are valid.
                binding_path = core_home_a / "shared-bridge-binding.json"
                stale = json.loads(binding_path.read_text(encoding="utf-8"))
                stale["workdirs"][1]["policy_revision"] = "f" * 64
                binding_path.write_text(json.dumps(stale), encoding="utf-8")
                with pytest.raises(SharedCoreBootstrapError):
                    await prepare_shared_agent_adapter(
                        identity_a, config_path, socket_path, home=home, timeout_seconds=3
                    )
                assert (await adapter.call("task.get", {"task_id": second_id}))[
                    "task_id"
                ] == second_id
            assert await adapter.call("bridge.ping", {}) == {"status": "ok"}
        finally:
            await server.close()

    try:
        asyncio.run(exercise())
    finally:
        shutil.rmtree(root, ignore_errors=True)

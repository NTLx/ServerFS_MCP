"""Opt-in named Shared Bridge bootstrap tests with synthetic temporary state."""

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
from serverfs_mcp.darwin_instances import instance_service_home  # noqa: E402
from serverfs_mcp.darwin_shared_core_bootstrap import (  # noqa: E402
    SharedCoreBootstrapError,
    prepare_shared_v2_client,
)
from serverfs_mcp.instance_identity import InstanceIdentity  # noqa: E402

_SECRET = bytes(range(32))


def test_bootstrap_authenticates_loaded_grant_and_fails_closed_without_stopping_bridge() -> None:
    root = Path("/private/tmp") / f"sfsb-{uuid.uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    home = root / "user-home"
    home.mkdir(mode=0o700)
    core_home = instance_service_home(
        InstanceIdentity("core-a", "shared", "shared-bridge"), home=home
    )
    core_home.mkdir(parents=True, mode=0o700)
    workdir = root / "workspace"
    workdir.mkdir(mode=0o700)
    socket_path = root / "bridge.sock"
    identity = InstanceIdentity("core-a", "shared", "shared-bridge")

    async def exercise() -> None:
        policy = WorkdirAgentPolicy(
            slot=1,
            alias="Workspace",
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
                    "core-a",
                    "core-key",
                    _SECRET,
                    (("Workspace", digest),),
                    frozenset({"bridge.ping"}),
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
        config_path = root / "core.toml"
        config_path.write_text(
            "[agent]\nenabled = true\n[agent.codex]\nenabled = true\n"
            "[[workdirs]]\nalias = 'Workspace'\n"
            f"path = '{workdir}'\nread_only = false\n"
            "agent_mode = 'workspace-write'\nagent_runtimes = ['codex']\n",
            encoding="utf-8",
        )
        config_path.chmod(0o600)
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
        binding = {
            "schema_version": 1,
            "bridge_id": "shared-bridge",
            "core_id": "core-a",
            "key_id": "core-key",
            "secret_hex": _SECRET.hex(),
            "workdir_alias": "Workspace",
            "workdir_path": str(workdir),
            "policy_revision": digest,
            "bridge_config_path": str(bridge_config),
            "socket_path": str(socket_path),
        }

        def write_binding(value: dict[str, object]) -> None:
            binding_path.write_text(json.dumps(value), encoding="utf-8")
            binding_path.chmod(0o600)

        try:
            write_binding(binding)
            client = await prepare_shared_v2_client(
                identity, config_path, socket_path, home=home, timeout_seconds=2
            )
            assert isinstance(client, CoreBridgeClientV2)
            assert client._secret == _SECRET
            assert _SECRET.hex() not in repr(client)
            assert await client.call("bridge.ping", {}) == {"status": "ok"}

            binding_path.unlink()
            with pytest.raises(SharedCoreBootstrapError) as missing:
                await prepare_shared_v2_client(identity, config_path, socket_path, home=home)
            assert str(core_home) not in str(missing.value)
            assert socket_path.exists()

            write_binding({**binding, "policy_revision": "f" * 64})
            with pytest.raises(SharedCoreBootstrapError):
                await prepare_shared_v2_client(identity, config_path, socket_path, home=home)
            assert socket_path.exists()

            write_binding({**binding, "secret_hex": (b"x" * 32).hex()})
            with pytest.raises(SharedCoreBootstrapError) as wrong_key:
                await prepare_shared_v2_client(identity, config_path, socket_path, home=home)
            assert _SECRET.hex() not in str(wrong_key.value)
            assert socket_path.exists()

            # Failed bootstrap did not stop or mutate the independently owned Bridge.
            assert await client.call("bridge.ping", {}) == {"status": "ok"}

            write_binding(binding)
            with pytest.raises(SharedCoreBootstrapError):
                await prepare_shared_v2_client(
                    identity,
                    config_path,
                    socket_path,
                    home=root / "attacker-home",
                )
            with pytest.raises(SharedCoreBootstrapError):
                await prepare_shared_v2_client(
                    InstanceIdentity("core-b", "shared", "shared-bridge"),
                    config_path,
                    socket_path,
                    home=home,
                )
            with pytest.raises(SharedCoreBootstrapError):
                await prepare_shared_v2_client(
                    identity,
                    config_path,
                    root / "other.sock",
                    home=home,
                )
            for invalid_timeout in (
                0,
                -1,
                10**1000,
                float("inf"),
                float("nan"),
                True,
                "1",
            ):
                with pytest.raises(SharedCoreBootstrapError):
                    await prepare_shared_v2_client(
                        identity,
                        config_path,
                        socket_path,
                        home=home,
                        timeout_seconds=invalid_timeout,  # type: ignore[arg-type]
                    )
            assert await client.call("bridge.ping", {}) == {"status": "ok"}
        finally:
            await server.close()

    try:
        assert len(os.fsencode(str(socket_path))) < 104
        asyncio.run(exercise())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_bootstrap_does_not_start_or_register_a_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing owner binding fails before any service-side operation exists."""
    root = Path("/private/tmp") / f"sfsb-{uuid.uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    home = root / "home"
    home.mkdir(mode=0o700)
    identity = InstanceIdentity("core-a", "shared", "shared-bridge")
    config = root / "core.toml"
    config.write_text("", encoding="utf-8")
    config.chmod(0o600)
    calls: list[str] = []

    def forbidden_lifecycle(*args, **kwargs):
        calls.append("lifecycle")
        raise AssertionError("bootstrap must not invoke lifecycle")

    monkeypatch.setattr(
        "serverfs_mcp.darwin_shared_core_bootstrap.probe_shared_v2_bridge",
        forbidden_lifecycle,
    )
    try:
        with pytest.raises(SharedCoreBootstrapError):
            asyncio.run(prepare_shared_v2_client(identity, config, root / "bridge.sock", home=home))
        assert calls == []
    finally:
        shutil.rmtree(root, ignore_errors=True)

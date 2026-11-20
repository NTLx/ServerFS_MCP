from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

import serverfs_mcp.linux_v0250_core_binding as binding_module
import serverfs_mcp.main as main_module
from serverfs_mcp.config import Settings
from serverfs_mcp.linux_v0250_core_binding import (
    LinuxCoreBindingError,
    LinuxCoreBridgeUnavailable,
    load_linux_core_client,
    verify_linux_core_client,
)
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, Workdir, WorkdirRegistry

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only Core binding")


def _fixture(tmp_path: Path, **updates):
    work = tmp_path / "work"
    work.mkdir()
    binding_file = tmp_path / "core-bridge-binding.json"
    doc = {
        "schema_version": 1,
        "instance_id": "core-a",
        "core_id": "core-a",
        "bridge_id": "bridge-a",
        "key_id": "key-a",
        "secret_hex": bytes(range(32)).hex(),
        "workdir_alias": "Repo",
        "workdir_path": str(work),
        "policy_revision": "a" * 64,
        "socket_path": str(tmp_path / "bridge.sock"),
    }
    doc.update(updates)
    binding_file.write_text(json.dumps(doc), encoding="utf-8")
    binding_file.chmod(0o600)
    wd = Workdir(
        "Repo",
        work,
        None,
        policy=EffectiveWorkdirPolicy(
            agent_mode="review", agent_runtimes=frozenset({"codex"}), repo_ops_enabled=True
        ),
    )
    return binding_file, doc, [wd]


def _load(path, doc, workdirs):
    return load_linux_core_client(
        str(path),
        instance_id="core-a",
        bridge_id="bridge-a",
        socket_path=str(Path(path).parent / "bridge.sock"),
        workdirs=workdirs,
    )


def test_loads_single_private_grant_and_adapter_preserves_tools(tmp_path):
    path, doc, wds = _fixture(tmp_path)
    client, alias = _load(path, doc, wds)
    assert alias == "Repo"
    server = create_server(
        Settings(agent_bridge_enabled=True),
        WorkdirRegistry(wds),
        agent_client=__import__(
            "serverfs_mcp.linux_v0250_bridge_adapter", fromlist=["LinuxV0250BridgeAdapter"]
        ).LinuxV0250BridgeAdapter(client, alias),
    )
    assert {tool.name for tool in server._tool_manager.list_tools()} >= {
        "submit_agent_task",
        "inspect_repo",
        "push_repo",
    }
    assert bytes(range(32)).hex() not in repr(client)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "symlink",
        "parent_symlink",
        "mode",
        "duplicate",
        "alias",
        "path",
        "name",
        "instance",
        "core",
        "bridge",
        "socket",
        "many",
    ],
)
def test_invalid_binding_fails_closed(tmp_path, mutation):
    path, doc, wds = _fixture(tmp_path)
    if mutation == "missing":
        path.unlink()
    elif mutation == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    elif mutation == "parent_symlink":
        real_parent = tmp_path / "real-parent"
        real_parent.mkdir(mode=0o700)
        real_file = real_parent / path.name
        path.rename(real_file)
        link_parent = tmp_path / "link-parent"
        link_parent.symlink_to(real_parent, target_is_directory=True)
        path = link_parent / path.name
    elif mutation == "mode":
        path.chmod(0o644)
    elif mutation == "duplicate":
        path.write_text(json.dumps(doc)[:-1] + ',"schema_version":1}')
    elif mutation in {"alias", "path"}:
        key = "workdir_alias" if mutation == "alias" else "workdir_path"
        doc[key] = "wrong" if mutation == "alias" else str(tmp_path)
    elif mutation == "name":
        path = path.with_name("unexpected-binding.json")
    elif mutation in {"instance", "core", "bridge", "socket"}:
        key = {
            "instance": "instance_id",
            "core": "core_id",
            "bridge": "bridge_id",
            "socket": "socket_path",
        }[mutation]
        doc[key] = "wrong" if mutation != "socket" else str(tmp_path / "wrong.sock")
    elif mutation == "many":
        second = tmp_path / "second"
        second.mkdir()
        wds.append(
            Workdir("Other", second, None, agent_mode="review", agent_runtimes=frozenset({"codex"}))
        )
    if mutation not in {"missing", "symlink", "parent_symlink", "mode", "name", "duplicate"}:
        path.write_text(json.dumps(doc), encoding="utf-8")
        path.chmod(0o600)
    with pytest.raises(LinuxCoreBindingError):
        _load(path, doc, wds)


def test_binding_diagnostics_are_fixed_and_fifo_is_rejected(tmp_path):
    path, doc, wds = _fixture(tmp_path)
    path.chmod(0o644)
    with pytest.raises(LinuxCoreBindingError, match="private-file"):
        _load(path, doc, wds)
    path.unlink()
    os.mkfifo(path, 0o600)
    with pytest.raises(LinuxCoreBindingError, match="private-file"):
        _load(path, doc, wds)
    path.unlink()
    path.write_text(json.dumps({**doc, "workdir_alias": "Different"}))
    path.chmod(0o600)
    with pytest.raises(LinuxCoreBindingError, match="workdir-binding") as exc:
        _load(path, doc, wds)
    assert str(tmp_path) not in str(exc.value)
    assert doc["secret_hex"] not in str(exc.value)


def test_readiness_rejection_or_unavailable_is_sanitized(tmp_path):
    path, doc, wds = _fixture(tmp_path)
    client, _ = _load(path, doc, wds)
    with pytest.raises(LinuxCoreBridgeUnavailable, match="ping-unavailable"):
        asyncio.run(verify_linux_core_client(client, Path(doc["socket_path"])))
    with pytest.raises(LinuxCoreBindingError, match="socket-binding") as exc:
        asyncio.run(verify_linux_core_client(client, Path("/tmp/other.sock")))
    assert str(tmp_path) not in str(exc.value)


def test_private_file_rejects_wrong_owner_and_multiple_links(tmp_path, monkeypatch):
    path, doc, wds = _fixture(tmp_path)
    actual_uid = os.getuid()
    with monkeypatch.context() as scoped:
        scoped.setattr(binding_module.os, "getuid", lambda: actual_uid + 1)
        with pytest.raises(LinuxCoreBindingError):
            _load(path, doc, wds)
    os.link(path, tmp_path / "second-link")
    with pytest.raises(LinuxCoreBindingError):
        _load(path, doc, wds)


def test_disabled_bridge_binding_cannot_construct_public_server():
    from serverfs_mcp.linux_v0250_bridge_adapter import LinuxV0250BridgeAdapter

    class FakeClient:
        _workdir_alias = "Repo"

        async def call(self, method, params):
            return {"status": "ok"}

    wd = Workdir(
        "Repo",
        Path("/tmp"),
        None,
        policy=EffectiveWorkdirPolicy(
            agent_mode="review", agent_runtimes=frozenset({"codex"}), repo_ops_enabled=True
        ),
    )
    with pytest.raises(ValueError, match="requires the host Bridge"):
        create_server(
            Settings(),
            WorkdirRegistry([wd]),
            agent_client=LinuxV0250BridgeAdapter(FakeClient(), "Repo"),
        )


def test_requested_v2_binding_failure_never_constructs_v1_server(tmp_path, monkeypatch):
    path, _doc, wds = _fixture(tmp_path)
    settings = Settings(agent_bridge_enabled=True, linux_v0250_bridge_binding_file=str(path))
    monkeypatch.setattr(main_module, "settings_from_env", lambda _env: settings)
    monkeypatch.setattr(
        main_module,
        "build_registry_from_env",
        lambda _env, _settings: WorkdirRegistry(wds),
    )
    called = False

    def create_server_spy(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("server construction must not fall back to v1")

    monkeypatch.setattr(main_module, "create_server", create_server_spy)
    monkeypatch.setenv("SERVERFS_LINUX_INSTANCE_ID", "")
    monkeypatch.setenv("SERVERFS_LINUX_BRIDGE_ID", "")
    assert main_module.main() == 2
    assert not called


@pytest.mark.parametrize("reject", [False, True])
def test_main_preserves_signed_client_during_unavailability(tmp_path, monkeypatch, reject):
    from serverfs_mcp.core_bridge_client_v2 import CoreBridgeRemoteError
    from serverfs_mcp.linux_v0250_bridge_adapter import LinuxV0250BridgeAdapter

    path, doc, wds = _fixture(tmp_path)
    settings = Settings(
        agent_bridge_enabled=True,
        linux_v0250_bridge_binding_file=str(path),
        agent_bridge_socket=doc["socket_path"],
        http_api_key="fixture-token",
    )
    registry = WorkdirRegistry(wds)
    monkeypatch.setattr(main_module, "settings_from_env", lambda _env: settings)
    monkeypatch.setattr(main_module, "build_registry_from_env", lambda *_: registry)
    monkeypatch.setenv("SERVERFS_LINUX_INSTANCE_ID", "core-a")
    monkeypatch.setenv("SERVERFS_LINUX_BRIDGE_ID", "bridge-a")

    def forbid_v1(*_args, **_kwargs):
        pytest.fail("explicit v2 configuration must not construct v1 client")

    monkeypatch.setattr(main_module, "AgentBridgeClient", forbid_v1)
    if reject:

        async def denied(*_args, **_kwargs):
            raise CoreBridgeRemoteError("CORE_AUTH_DENIED", "rejected")

        monkeypatch.setattr(binding_module.CoreBridgeClientV2, "call", denied)

    clients = []

    def create(_settings, _registry, client, _ingress):
        clients.append(client)
        return object()

    monkeypatch.setattr(main_module, "create_server", create)
    monkeypatch.setattr(main_module, "run_api_key_streamable_http", lambda *_: None)
    assert main_module.main() == (2 if reject else 0)
    if reject:
        assert not clients
    else:
        assert len(clients) == 1
        assert isinstance(clients[0], LinuxV0250BridgeAdapter)
        assert clients[0]._workdir_alias == "Repo"

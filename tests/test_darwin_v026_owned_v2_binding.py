"""macOS named-owned v2 grant binding staging contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.agent_client import AgentBridgeClientError
from serverfs_mcp.darwin_instance_bridge_identity import named_owned_bridge_resources
from serverfs_mcp.darwin_instance_owned_staging import stage_owned_v2_grant_binding
from serverfs_mcp.darwin_owned_v2_agent_adapter import DarwinOwnedV2AgentAdapter
from serverfs_mcp.darwin_owned_v2_binding import (
    generate_owned_v2_grant_materials,
    write_owned_v2_binding,
)
from serverfs_mcp.darwin_owned_v2_bootstrap import (
    DarwinOwnedV2BootstrapError,
    _pairs,
    _read_private_json,
    load_owned_v2_clients,
)
from serverfs_mcp.instance_identity import InstanceIdentity


def test_owned_v2_binding_is_per_alias_private_and_self_scoped(tmp_path) -> None:
    identity = InstanceIdentity("owned-v2", "owned", "owned-v2")
    home = tmp_path / "user"
    owner_root = (
        home
        / "Library"
        / "Application Support"
        / "ServerFS"
        / "instances"
        / "owned-v2"
        / "service-manager"
    )
    owner_root.mkdir(parents=True, mode=0o700)
    owner_root.chmod(0o700)
    repo = tmp_path / "repo"
    notes = tmp_path / "notes"
    files = tmp_path / "files"
    for path in (repo, notes, files):
        path.mkdir()
    materials = generate_owned_v2_grant_materials(identity, ("repo", "notes"))
    scopes = [
        {"alias": "repo", "key_id": materials[0].key_id, "policy_revision": "a" * 64},
        {"alias": "notes", "key_id": materials[1].key_id, "policy_revision": "b" * 64},
    ]
    bridge_config = tmp_path / "bridge.json"
    socket = tmp_path / "bridge.sock"

    binding_path = write_owned_v2_binding(
        identity,
        home=home,
        bridge_config_path=bridge_config,
        socket_path=socket,
        agent_workdirs={"repo": repo, "notes": notes},
        materials=materials,
        grant_scopes=scopes,
    )

    assert stat.S_IMODE(binding_path.stat().st_mode) == 0o600
    binding = json.loads(binding_path.read_text())
    assert binding["core_id"] == binding["bridge_id"] == binding["instance_id"] == "owned-v2"
    assert binding["bridge_config_path"] == str(bridge_config)
    assert binding["socket_path"] == str(socket)
    entries = {item["alias"]: item for item in binding["workdirs"]}
    assert set(entries) == {"repo", "notes"}
    assert entries["repo"]["path"] == str(repo.resolve())
    assert entries["notes"]["path"] == str(notes.resolve())
    assert entries["repo"]["key_id"] != entries["notes"]["key_id"]
    assert entries["repo"]["secret_hex"] != entries["notes"]["secret_hex"]
    assert entries["repo"]["policy_revision"] == "a" * 64
    assert entries["notes"]["policy_revision"] == "b" * 64
    assert str(files) not in binding_path.read_text()


@pytest.mark.parametrize(
    "wrong_identity",
    [
        InstanceIdentity("owned-v2", "shared", "other-bridge"),
    ],
)
def test_owned_v2_material_generation_refuses_non_self_identity(wrong_identity) -> None:
    with pytest.raises(ValueError):
        generate_owned_v2_grant_materials(wrong_identity, ("repo",))


def test_owned_v2_binding_refuses_scope_mismatch_and_never_overwrites(tmp_path) -> None:
    identity = InstanceIdentity("owned-v2", "owned", "owned-v2")
    home = tmp_path / "user"
    owner_root = (
        home
        / "Library"
        / "Application Support"
        / "ServerFS"
        / "instances"
        / "owned-v2"
        / "service-manager"
    )
    owner_root.mkdir(parents=True, mode=0o700)
    owner_root.chmod(0o700)
    repo = tmp_path / "repo"
    repo.mkdir()
    materials = generate_owned_v2_grant_materials(identity, ("repo",))
    binding_path = owner_root / "owned-bridge-v2-binding.json"
    with pytest.raises(ValueError):
        write_owned_v2_binding(
            identity,
            home=home,
            bridge_config_path=tmp_path / "bridge.json",
            socket_path=tmp_path / "bridge.sock",
            agent_workdirs={"repo": repo},
            materials=materials,
            grant_scopes=[],
        )
    assert not binding_path.exists()

    valid_scope = [{"alias": "repo", "key_id": materials[0].key_id, "policy_revision": "a" * 64}]
    write_owned_v2_binding(
        identity,
        home=home,
        bridge_config_path=tmp_path / "bridge.json",
        socket_path=tmp_path / "bridge.sock",
        agent_workdirs={"repo": repo},
        materials=materials,
        grant_scopes=valid_scope,
    )
    original = binding_path.read_bytes()
    with pytest.raises(FileExistsError):
        write_owned_v2_binding(
            identity,
            home=home,
            bridge_config_path=tmp_path / "other-bridge.json",
            socket_path=tmp_path / "other.sock",
            agent_workdirs={"repo": repo},
            materials=materials,
            grant_scopes=valid_scope,
        )
    assert binding_path.read_bytes() == original


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS staging only")
@pytest.mark.parametrize("workdir_collection", [list, tuple])
def test_owned_v2_staging_passes_material_to_bridge_helper_and_persists_binding(
    tmp_path, monkeypatch, workdir_collection
) -> None:
    bridge_src = Path(__file__).resolve().parents[1] / "agent_bridge" / "src"
    if str(bridge_src) not in sys.path:
        sys.path.insert(0, str(bridge_src))
    from serverfs_agent_bridge import core_grants
    from serverfs_agent_bridge.macos_owned_config import stage_named_owned_v2_config

    identity = InstanceIdentity("owned-v2", "owned", "owned-v2")
    home = tmp_path / "user"
    owner_root = home / "Library" / "Application Support" / "ServerFS" / "instances" / "owned-v2"
    service_root = owner_root / "service-manager"
    data_root = service_root / "data"
    data_root.mkdir(parents=True, mode=0o700)
    for path in (owner_root.parent, owner_root, service_root, data_root):
        path.chmod(0o700)
    resources = named_owned_bridge_resources(identity, home=home)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    file_only = tmp_path / "files"
    file_only.mkdir()
    workdirs = (
        SimpleNamespace(
            alias="repo",
            root=workdir,
            read_only=False,
            agent_mode="review",
            policy=SimpleNamespace(repo_ops_enabled=False),
        ),
        SimpleNamespace(
            alias="files",
            root=file_only,
            read_only=True,
            agent_mode="disabled",
            policy=SimpleNamespace(repo_ops_enabled=False),
        ),
    )
    workdirs = workdir_collection(workdirs)
    request = {
        "enable_fake_runtime": True,
        "workdirs": [
            {
                "alias": "repo",
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "review",
                "agent_runtimes": ["fake"],
            },
            {
                "alias": "files",
                "host_path": str(file_only),
                "read_only": True,
                "agent_mode": "disabled",
                "agent_runtimes": [],
            },
        ],
    }
    monkeypatch.setattr(
        core_grants, "_WORKDIR_METHODS", core_grants._WORKDIR_METHODS | {"task.list"}
    )
    monkeypatch.setattr(
        "serverfs_mcp.darwin_instance_owned_staging.preflight_named_owned_bridge",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "serverfs_mcp.darwin_instance_owned_staging.load_native_config",
        lambda _path: (workdirs, object()),
    )
    monkeypatch.setattr(
        "serverfs_mcp.darwin_instance_owned_staging.render_request_from_config",
        lambda _workdirs, _settings: request,
    )
    monkeypatch.setattr(
        "serverfs_agent_bridge.macos_owned_config.named_socket_path",
        lambda _identifier: resources.socket_path,
    )

    def fake_bridge_subprocess(_argv, *, input, **_kwargs):
        envelope = json.loads(input)
        result = stage_named_owned_v2_config(
            identity.instance_id,
            resources.config_path.parent,
            envelope["policy"],
            envelope["grant_materials"],
            allow_test_fake=True,
        )
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))

    monkeypatch.setattr(
        "serverfs_mcp.darwin_instance_owned_staging.subprocess.run", fake_bridge_subprocess
    )
    monkeypatch.setattr(
        "serverfs_mcp.darwin_instance_owned_staging.preflight_named_owned_bridge",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "serverfs_agent_bridge.macos_owned_config.named_socket_path",
        lambda _identifier: resources.socket_path,
    )

    result = stage_owned_v2_grant_binding(
        identity,
        tmp_path / "serverfs.toml",
        Path(sys.executable),
        workdirs,
        home=home,
    )

    assert result["protocol_mode"] == "v2"
    binding_path = service_root / "owned-bridge-v2-binding.json"
    binding = json.loads(binding_path.read_text())
    assert {entry["alias"] for entry in binding["workdirs"]} == {"repo"}
    assert binding["core_id"] == binding["bridge_id"] == identity.instance_id


def test_owned_v2_loader_fails_closed_for_missing_binding_and_foreign_instance(
    tmp_path, monkeypatch
) -> None:
    import serverfs_mcp.darwin_owned_v2_bootstrap as bootstrap

    identity = InstanceIdentity("owned-v2", "owned", "owned-v2")
    home = tmp_path / "user"
    root = (
        home
        / "Library"
        / "Application Support"
        / "ServerFS"
        / "instances"
        / "owned-v2"
        / "service-manager"
    )
    root.mkdir(parents=True)
    root.chmod(0o700)
    monkeypatch.setattr(bootstrap, "instance_service_home", lambda *_args, **_kwargs: root)
    with pytest.raises(DarwinOwnedV2BootstrapError):
        load_owned_v2_clients(identity, tmp_path / "serverfs.toml", home=home)
    with pytest.raises(DarwinOwnedV2BootstrapError):
        load_owned_v2_clients(
            InstanceIdentity("another-instance", "owned", "another-instance"),
            tmp_path / "serverfs.toml",
            home=home,
        )


def test_owned_v2_loader_rejects_duplicate_json_keys_and_symlinked_parent(tmp_path) -> None:
    with pytest.raises(DarwinOwnedV2BootstrapError):
        _pairs([("core_id", "owned-v2"), ("core_id", "other")])

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    target = private / "binding.json"
    target.write_text('{"owner":"test"}')
    target.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)
    with pytest.raises(OSError):
        _read_private_json(alias / "binding.json", max_bytes=1024)


@pytest.mark.parametrize("bridge_mode", ["v1", "v2"])
def test_owned_v2_loader_rejects_legacy_or_missing_task_list(
    tmp_path, monkeypatch, bridge_mode
) -> None:
    import serverfs_mcp.darwin_owned_v2_bootstrap as bootstrap

    identity = InstanceIdentity("owned-v2", "owned", "owned-v2")
    home = tmp_path / "user"
    root = (
        home
        / "Library"
        / "Application Support"
        / "ServerFS"
        / "instances"
        / "owned-v2"
        / "service-manager"
    )
    root.mkdir(parents=True)
    root.chmod(0o700)
    binding_path = root / "owned-bridge-v2-binding.json"
    bridge_dir = tmp_path / "bridge-private"
    bridge_dir.mkdir(mode=0o700)
    bridge_dir.chmod(0o700)
    bridge_config = bridge_dir / "bridge.json"
    socket_path = tmp_path / "bridge.sock"
    core_config = tmp_path / "serverfs.toml"
    core_config.write_text("[server]\n")
    bridge_config.write_text(
        json.dumps(
            {
                "protocol_mode": bridge_mode,
                "bridge_id": identity.instance_id if bridge_mode == "v2" else None,
                "socket_path": str(socket_path),
            }
        )
    )
    bridge_config.chmod(0o600)
    workdir_entries = []
    workdirs = ()
    settings = SimpleNamespace(bridge_required=False)
    if bridge_mode == "v2":
        repo = tmp_path / "repo"
        repo.mkdir()
        workdirs = (
            SimpleNamespace(
                alias="repo",
                root=repo,
                agent_mode="review",
                policy=SimpleNamespace(repo_ops_enabled=False),
            ),
        )
        settings.bridge_required = True
        workdir_entries = [
            {
                "alias": "repo",
                "path": str(repo),
                "key_id": "own-0123456789abcdef",
                "secret_hex": "a" * 64,
                "policy_revision": "b" * 64,
            }
        ]
    binding_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "v0.26-named-owned-core-v2",
                "instance_id": identity.instance_id,
                "core_id": identity.instance_id,
                "bridge_id": identity.instance_id,
                "bridge_config_path": str(bridge_config),
                "socket_path": str(socket_path),
                "workdirs": workdir_entries,
            }
        )
    )
    binding_path.chmod(0o600)
    (root / "services.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "v0.25-named-owned-core",
                "instance_id": identity.instance_id,
                "bridge_mode": "owned",
                "bridge_id": identity.instance_id,
                "core": {
                    "config": str(core_config),
                    "sha256": hashlib.sha256(core_config.read_bytes()).hexdigest(),
                    "port": 8080,
                    "hostname": "localhost",
                },
                "services": {
                    "http-origin": {
                        "executable": "/usr/bin/python3",
                        "environment_file": str(root / "http-origin-environment.json"),
                        "arguments": [
                            "-m",
                            "serverfs_mcp.cli",
                            "serve",
                            "--transport",
                            "streamable-http",
                            "--config",
                            str(core_config),
                            "--http-port",
                            "8080",
                            "--macos-instance",
                            identity.instance_id,
                            "--macos-agent-protocol",
                            "v2",
                        ],
                        "origin_owner": "independent-core",
                    },
                    "agent-bridge": {
                        "executable": "/usr/bin/python3",
                        "protocol_mode": "v2",
                        "bridge_config": str(bridge_config),
                        "environment_file": None,
                        "working_directory": str(root / "data"),
                        "origin_owner": "independent-bridge",
                        "arguments": [
                            "-B",
                            "-m",
                            "serverfs_mcp.darwin_owned_bridge_supervisor",
                            "--instance",
                            identity.instance_id,
                        ],
                    },
                },
            }
        )
    )
    (root / "services.json").chmod(0o600)
    data_root = root / "data"
    data_root.mkdir(mode=0o700)
    data_root.chmod(0o700)
    environment_path = root / "http-origin-environment.json"
    environment_path.write_text(
        json.dumps(
            {
                "SERVERFS_DATA_HOME": str(data_root),
                "SERVERFS_FILE_INGRESS_ENABLED": "false",
            }
        )
    )
    environment_path.chmod(0o600)
    resources = SimpleNamespace(config_path=bridge_config, socket_path=socket_path)
    monkeypatch.setattr(bootstrap, "instance_service_home", lambda *_args, **_kwargs: root)
    monkeypatch.setattr(
        bootstrap, "named_owned_bridge_resources", lambda *_args, **_kwargs: resources
    )
    monkeypatch.setattr(bootstrap, "load_native_config", lambda _path: (workdirs, settings))
    if bridge_mode == "v2":
        monkeypatch.setattr(bootstrap.core_bridge_client_v2, "_METHODS", frozenset({"bridge.ping"}))
    with pytest.raises(DarwinOwnedV2BootstrapError):
        load_owned_v2_clients(identity, core_config, home=home)
    if bridge_mode == "v2":
        monkeypatch.setattr(
            bootstrap.core_bridge_client_v2,
            "_METHODS",
            bootstrap.core_bridge_client_v2._METHODS | {"task.list"},
        )
        assert set(load_owned_v2_clients(identity, core_config, home=home)) == {"repo"}

        # Connector-capable v2 must reuse the original manifest safety checks.
        manifest_path = root / "services.json"
        original_manifest = json.loads(manifest_path.read_text())
        manifest = json.loads(manifest_path.read_text())
        manifest["services"]["unexpected"] = {}
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(DarwinOwnedV2BootstrapError):
            load_owned_v2_clients(identity, core_config, home=home)
        del manifest["services"]["unexpected"]
        manifest["services"]["cloudflare"] = {"core_port": 8081}
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(DarwinOwnedV2BootstrapError):
            load_owned_v2_clients(identity, core_config, home=home)
        manifest_path.write_text(json.dumps(original_manifest))

        original_binding = json.loads(binding_path.read_text())
        altered_binding = json.loads(binding_path.read_text())
        altered_binding["workdirs"][0]["alias"] = "other"
        binding_path.write_text(json.dumps(altered_binding))
        binding_path.chmod(0o600)
        with pytest.raises(DarwinOwnedV2BootstrapError):
            load_owned_v2_clients(identity, core_config, home=home)
        binding_path.write_text(json.dumps(original_binding))
        binding_path.chmod(0o600)

        monkeypatch.setattr(
            bootstrap,
            "load_native_config",
            lambda _path: ((), SimpleNamespace(bridge_required=False)),
        )
        file_only_binding = json.loads(binding_path.read_text())
        file_only_binding["workdirs"] = []
        binding_path.write_text(json.dumps(file_only_binding))
        binding_path.chmod(0o600)
        with pytest.raises(DarwinOwnedV2BootstrapError):
            load_owned_v2_clients(identity, core_config, home=home)
        binding_path.write_text(json.dumps(original_binding))
        binding_path.chmod(0o600)
        monkeypatch.setattr(bootstrap, "load_native_config", lambda _path: (workdirs, settings))

        manifest_path = root / "services.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["instance_id"] = "foreign-core"
        manifest_path.write_text(json.dumps(manifest))
        manifest_path.chmod(0o600)
        with pytest.raises(DarwinOwnedV2BootstrapError):
            load_owned_v2_clients(identity, core_config, home=home)


def test_owned_v2_agent_adapter_keeps_alias_scope_and_refuses_unsupported_submit_options() -> None:
    class Client:
        _workdir_alias = "Repo"

        def __init__(self) -> None:
            self.calls = []

        async def call(self, method: str, params: dict) -> dict:
            self.calls.append((method, dict(params)))
            return {"method": method, "params": params}

    client = Client()
    adapter = DarwinOwnedV2AgentAdapter({"Repo": client})
    result = asyncio.run(
        adapter.call("task.list", {"workdir": "Repo", "status": "active", "limit": 5})
    )
    assert result == {"method": "task.list", "params": {"status": "active", "limit": 5}}
    assert client.calls == [("task.list", {"status": "active", "limit": 5})]
    with pytest.raises(AgentBridgeClientError):
        asyncio.run(
            adapter.call(
                "task.submit",
                {
                    "workdir": "Repo",
                    "runtime": "codex",
                    "continue_from_task_id": "x",
                },
            )
        )
    with pytest.raises(AgentBridgeClientError):
        asyncio.run(
            adapter.call(
                "task.submit", {"workdir": "Repo", "runtime": "codex", "idempotency_key": "x"}
            )
        )
    assert len(client.calls) == 1

    repo = Client()
    mixed = DarwinOwnedV2AgentAdapter(
        {"Repo": client, "OnlyRepo": repo},
        agent_aliases={"Repo"},
        repo_aliases={"OnlyRepo"},
    )
    assert asyncio.run(
        mixed.call("repo.inspect", {"workdir": "OnlyRepo", "view": "actions"})
    ) == {"method": "repo.inspect", "params": {"view": "actions"}}
    for method, params in (
        ("repo.inspect", {"workdir": "Repo", "view": "actions"}),
        ("repo.inspect", {"view": "actions"}),
        ("task.list", {"workdir": "OnlyRepo"}),
    ):
        with pytest.raises(AgentBridgeClientError):
            asyncio.run(mixed.call(method, params))
    asyncio.run(mixed.call("task.get", {"task_id": "agt_test"}))
    assert repo.calls == [("repo.inspect", {"view": "actions"})]
    repo_only = DarwinOwnedV2AgentAdapter(
        {"OnlyRepo": repo}, agent_aliases=set(), repo_aliases={"OnlyRepo"}
    )
    with pytest.raises(AgentBridgeClientError):
        asyncio.run(repo_only.call("task.get", {"task_id": "agt_test"}))


def test_owned_runtime_v2_stage_is_explicit_and_records_protocol_marker(
    tmp_path, monkeypatch
) -> None:
    import serverfs_mcp.darwin_instance_bridge_identity as bridge_identity
    import serverfs_mcp.darwin_instance_owned_runtime as owned_runtime
    import serverfs_mcp.darwin_instance_owned_staging as owned_staging

    identity = InstanceIdentity("owned-v2", "owned", "owned-v2")
    home = tmp_path / "user"
    base = home / "Library" / "Application Support" / "ServerFS"
    base.mkdir(parents=True)
    config = tmp_path / "serverfs.toml"
    config.write_text("[server]\n")
    env_file = tmp_path / ".env"
    env_file.write_text("SERVERFS_HTTP_API_KEY=test\n")
    template = tmp_path / "ServerFS.app"
    bridge_python = Path(sys.executable)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    workdirs = (SimpleNamespace(alias="Repo", root=workdir, agent_mode="review"),)
    resources = SimpleNamespace(
        config_path=base
        / "instances"
        / identity.instance_id
        / "service-manager"
        / "data"
        / "agent-bridge"
        / "state"
        / "bridge.json",
        socket_path=tmp_path / "bridge.sock",
    )
    staged_scopes = []
    monkeypatch.setattr(owned_runtime, "_assert_available_port", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        owned_runtime,
        "_prepare_file_only",
        lambda *_args, **_kwargs: (
            {
                "services": {
                    "http-origin": {
                        "arguments": [
                            "-m",
                            "serverfs_mcp.cli",
                            "serve",
                            "--transport",
                            "streamable-http",
                            "--config",
                            str(config),
                            "--http-port",
                            "8765",
                            "--macos-instance",
                            identity.instance_id,
                        ]
                    }
                }
            },
            b"{}",
        ),
    )
    monkeypatch.setattr(owned_runtime, "_verify_source_app", lambda _path: None)
    monkeypatch.setattr(owned_runtime, "stage_instance_app", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(owned_runtime, "preflight_named_owned_bridge", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        bridge_identity, "named_owned_bridge_resources", lambda *_args, **_kwargs: resources
    )
    monkeypatch.setattr(
        "serverfs_mcp.native_config.load_native_config", lambda _path: (workdirs, object())
    )
    monkeypatch.setattr(
        owned_staging,
        "stage_owned_v2_grant_binding",
        lambda *_args, **_kwargs: staged_scopes.append("v2"),
    )
    monkeypatch.setattr(
        "serverfs_mcp.darwin_owned_v2_bootstrap.load_owned_v2_clients",
        lambda *_args, **_kwargs: {"Repo": object()},
    )

    result = owned_runtime.stage_owned_core_bridge(
        identity,
        config,
        env_file,
        8765,
        "localhost",
        template,
        bridge_python,
        agent_protocol="v2",
        home=home,
    )

    assert result["state"] == "staged-not-registered"
    assert staged_scopes == ["v2"]
    manifest_path = base / "instances" / identity.instance_id / "service-manager" / "services.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["services"]["agent-bridge"]["protocol_mode"] == "v2"
    assert manifest["services"]["http-origin"]["arguments"][-2:] == [
        "--macos-agent-protocol",
        "v2",
    ]

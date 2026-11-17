"""Synthetic tests for the source-only named Shared Core manifest contract."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from serverfs_mcp.darwin_instance_shared_manifest import (
    DarwinSharedManifestError,
    resolve_shared_core_manifest,
)
from serverfs_mcp.darwin_instances import instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

PORT = 19283


def _setup(tmp_path: Path):
    user_home = tmp_path / "user"
    root = instance_service_home(
        InstanceIdentity("core-one", "shared", "bridge-one"), home=user_home
    )
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    config = tmp_path / "serverfs.toml"
    config.write_text(
        "[agent]\nenabled = true\n[agent.codex]\nenabled = true\n"
        "[[workdirs]]\nalias = 'workspace'\n"
        f"path = '{workspace}'\nread_only = false\n"
        "agent_mode = 'workspace-write'\nagent_runtimes = ['codex']\n"
    )
    config = config.resolve()
    binding_path = root / "shared-bridge-binding.json"
    bridge_config = root / "bridge.json"
    socket = tmp_path / "bridge.sock"
    bridge_config.write_text(
        json.dumps({"protocol_mode": "v2", "bridge_id": "bridge-one", "socket_path": str(socket)})
    )
    bridge_config.chmod(0o600)
    binding = {
        "schema_version": 1,
        "bridge_id": "bridge-one",
        "core_id": "core-one",
        "key_id": "key-one",
        "secret_hex": "a" * 64,
        "workdir_alias": "workspace",
        "workdir_path": str(workspace),
        "policy_revision": "b" * 64,
        "bridge_config_path": str(bridge_config),
        "socket_path": str(socket),
    }
    binding_path.write_text(json.dumps(binding))
    binding_path.chmod(0o600)
    snapshot = {
        "SERVERFS_MACOS_INSTANCE_ID": "core-one",
        "SERVERFS_DATA_HOME": str(root / "data"),
        "SERVERFS_FILE_INGRESS_ENABLED": "false",
        "SERVERFS_HTTP_API_KEY": "synthetic-only",
        "SERVERFS_HTTP_ALLOWED_HOSTS": f"core.example,127.0.0.1:{PORT}",
    }
    snapshot_path = root / "http-origin-environment.json"
    snapshot_path.write_text(json.dumps(snapshot))
    snapshot_path.chmod(0o600)
    manifest = {
        "schema_version": 1,
        "owner": "v0.25-named-shared-core",
        "instance_id": "core-one",
        "bridge_mode": "shared",
        "bridge_id": "bridge-one",
        "core": {
            "config": str(config),
            "sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "port": PORT,
            "hostname": "core.example",
        },
        "services": {
            "http-origin": {
                "executable": str(Path(sys.executable).absolute()),
                "arguments": [
                    "-B",
                    "-m",
                    "serverfs_mcp.cli",
                    "serve",
                    "--transport",
                    "streamable-http",
                    "--config",
                    str(config),
                    "--http-port",
                    str(PORT),
                    "--macos-instance",
                    "core-one",
                    "--macos-shared-bridge",
                    "bridge-one",
                ],
                "working_directory": str(config.parent),
                "environment_file": str(snapshot_path),
                "stdout_log": str(root / "logs/core.stdout.log"),
                "stderr_log": str(root / "logs/core.stderr.log"),
                "origin_owner": "independent-core",
            }
        },
    }
    manifest_path = root / "services.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    identity = InstanceIdentity("core-one", "shared", "bridge-one")
    environ = {
        "SERVERFS_MACOS_INSTANCE_ID": identity.instance_id,
        "SERVERFS_DATA_HOME": str(root / "data"),
        "SERVERFS_FILE_INGRESS_ENABLED": "false",
    }
    return identity, user_home, config, root, manifest, binding, snapshot, environ, socket


def _save_manifest(root: Path, document: dict) -> None:
    path = root / "services.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)


def test_valid_synthetic_manifest_returns_alias_and_owner_socket(tmp_path: Path) -> None:
    identity, home, config, root, _doc, _binding, _snapshot, env, socket = _setup(tmp_path)
    before = {
        p: (p.stat().st_mtime_ns, p.stat().st_size) for p in tmp_path.rglob("*") if p.is_file()
    }
    assert resolve_shared_core_manifest(identity, config, PORT, home=home, environ=env) == (
        "workspace",
        socket,
    )
    after = {
        p: (p.stat().st_mtime_ns, p.stat().st_size) for p in tmp_path.rglob("*") if p.is_file()
    }
    assert before == after
    assert root.is_dir()


def test_accepts_existing_named_core_metadata_shape(tmp_path: Path) -> None:
    identity, home, config, _root, doc, _binding, _snapshot, env, socket = _setup(tmp_path)
    assert set(doc["core"]) == {"config", "sha256", "port", "hostname"}
    assert doc["core"]["hostname"] == "core.example"
    assert resolve_shared_core_manifest(identity, config, PORT, home=home, environ=env) == (
        "workspace",
        socket,
    )


def test_shared_core_launch_requires_bytecode_suppression(tmp_path: Path) -> None:
    identity, home, config, root, doc, _binding, _snapshot, env, _socket = _setup(tmp_path)
    assert doc["services"]["http-origin"]["arguments"][:2] == ["-B", "-m"]
    doc["services"]["http-origin"]["arguments"].remove("-B")
    _save_manifest(root, doc)
    with pytest.raises(DarwinSharedManifestError, match="launch identity"):
        resolve_shared_core_manifest(identity, config, PORT, home=home, environ=env)


@pytest.mark.parametrize("hostname", [None, ""])
def test_rejects_missing_or_empty_core_hostname(tmp_path: Path, hostname: str | None) -> None:
    identity, home, config, root, doc, _binding, _snapshot, env, _socket = _setup(tmp_path)
    if hostname is None:
        del doc["core"]["hostname"]
    else:
        doc["core"]["hostname"] = hostname
    _save_manifest(root, doc)
    with pytest.raises(DarwinSharedManifestError) as excinfo:
        resolve_shared_core_manifest(identity, config, PORT, home=home, environ=env)
    assert "core.example" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stdout_log", "http-origin.stdout.log"),
        ("stderr_log", "http-origin.stderr.log"),
    ],
)
def test_rejects_http_origin_named_core_logs(tmp_path: Path, field: str, value: str) -> None:
    identity, home, config, root, doc, _binding, _snapshot, env, _socket = _setup(tmp_path)
    doc["services"]["http-origin"][field] = str(root / "logs" / value)
    _save_manifest(root, doc)
    with pytest.raises(DarwinSharedManifestError):
        resolve_shared_core_manifest(identity, config, PORT, home=home, environ=env)


@pytest.mark.parametrize(
    "case",
    [
        "drift",
        "wrong_core",
        "wrong_bridge",
        "extra_bridge",
        "wrong_env",
        "two_agent_workdirs",
        "repo_ops",
        "duplicate_key",
        "bad_mode",
    ],
)
def test_rejects_manifest_and_policy_drift(tmp_path: Path, case: str) -> None:
    identity, home, config, root, doc, binding, snapshot, env, _socket = _setup(tmp_path)
    if case == "drift":
        config.write_text(config.read_text() + "\n# drift\n")
    elif case == "wrong_core":
        doc["instance_id"] = "core-two"
    elif case == "wrong_bridge":
        doc["bridge_id"] = "bridge-two"
    elif case == "extra_bridge":
        doc["services"]["agent-bridge"] = {}
    elif case == "wrong_env":
        snapshot["SERVERFS_FILE_INGRESS_ENABLED"] = "true"
        path = root / "http-origin-environment.json"
        path.write_text(json.dumps(snapshot))
        path.chmod(0o600)
    elif case == "two_agent_workdirs":
        config.write_text(
            config.read_text()
            + "\n[[workdirs]]\nalias='other'\npath='"
            + str(tmp_path)
            + "'\nread_only=false\nagent_mode='workspace-write'\nagent_runtimes=['codex']\n"
        )
    elif case == "repo_ops":
        config.write_text(
            config.read_text().replace(
                "read_only = false", "read_only = false\nrepo_ops_enabled = true"
            )
        )
    elif case == "duplicate_key":
        path = root / "services.json"
        path.write_text(
            path.read_text().replace(
                '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
            )
        )
        path.chmod(0o600)
    elif case == "bad_mode":
        (root / "services.json").chmod(0o640)
    if case not in {
        "drift",
        "wrong_env",
        "two_agent_workdirs",
        "repo_ops",
        "duplicate_key",
        "bad_mode",
    }:
        _save_manifest(root, doc)
    if case in {"two_agent_workdirs", "repo_ops"}:
        doc["core"]["sha256"] = hashlib.sha256(config.read_bytes()).hexdigest()
        _save_manifest(root, doc)
    with pytest.raises(DarwinSharedManifestError):
        resolve_shared_core_manifest(identity, config, PORT, home=home, environ=env)


@pytest.mark.parametrize(
    "identity",
    [
        InstanceIdentity(),
        InstanceIdentity("only-owned", "owned", "only-owned"),
        InstanceIdentity("file-only"),
    ],
)
def test_rejects_default_and_owned_identity(tmp_path: Path, identity: InstanceIdentity) -> None:
    home = tmp_path / "user"
    config = tmp_path / "unused.toml"
    config.write_text("")
    with pytest.raises(DarwinSharedManifestError):
        resolve_shared_core_manifest(identity, config, PORT, home=home, environ={})


def test_rejects_symlinked_manifest_and_wrong_environment(tmp_path: Path) -> None:
    identity, home, config, root, _doc, _binding, _snapshot, env, _socket = _setup(tmp_path)
    path = root / "services.json"
    path.rename(root / "real.json")
    path.symlink_to(root / "real.json")
    with pytest.raises(DarwinSharedManifestError):
        resolve_shared_core_manifest(identity, config, PORT, home=home, environ=env)
    path.unlink()
    (root / "real.json").rename(path)
    path.chmod(0o600)
    with pytest.raises(DarwinSharedManifestError):
        resolve_shared_core_manifest(
            identity,
            config,
            PORT,
            home=home,
            environ={**env, "SERVERFS_MACOS_INSTANCE_ID": "other"},
        )


def test_rejects_non_0700_instance_root(tmp_path: Path) -> None:
    identity, home, config, root, _doc, _binding, _snapshot, env, _socket = _setup(tmp_path)
    root.chmod(0o500)
    with pytest.raises(DarwinSharedManifestError):
        resolve_shared_core_manifest(identity, config, PORT, home=home, environ=env)

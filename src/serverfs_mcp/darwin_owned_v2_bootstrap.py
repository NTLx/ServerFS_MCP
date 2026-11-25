"""Fail-closed loader for an explicitly staged macOS named-owned v2 binding."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from . import core_bridge_client_v2
from .core_bridge_client_v2 import CoreBridgeClientV2
from .darwin_instance_bridge_identity import named_owned_bridge_resources
from .darwin_instance_runtime import (
    _check_path,
    _service_supported,
    _verify_cloudflare_spec,
    _verify_openai_spec,
)
from .darwin_instances import instance_service_home
from .darwin_owned_v2_binding import _owned_identity
from .instance_identity import DEFAULT_INSTANCE_ID, InstanceIdentity
from .native_config import load_native_config

_FILENAME = "owned-bridge-v2-binding.json"
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 16 * 1024


class DarwinOwnedV2BootstrapError(ValueError):
    """The explicit named-owned v2 client could not be proven safe."""


def _deny() -> DarwinOwnedV2BootstrapError:
    return DarwinOwnedV2BootstrapError("named-owned v2 binding is invalid or unsupported")


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise _deny()
        result[key] = value
    return result


def _read_private_json(path: Path, *, max_bytes: int) -> dict:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise _deny()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(path.anchor, flags)
    try:
        for component in path.parent.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        parent = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077
        ):
            raise _deny()
        fd = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 2 <= info.st_size <= max_bytes
        ):
            raise _deny()
        content = bytearray()
        while len(content) <= max_bytes:
            chunk = os.read(fd, min(4096, max_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) != info.st_size or len(content) > max_bytes:
            raise _deny()
        document = json.loads(bytes(content).decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(document, dict):
            raise _deny()
        return document
    finally:
        os.close(fd)


def owned_protocol_marker(
    identity: InstanceIdentity, *, home: Path | None = None
) -> tuple[str, dict]:
    """Read the immutable v1/v2 selection from one private owned manifest."""
    try:
        _owned_identity(identity)
        root = instance_service_home(identity, home=home)
        manifest = _read_private_json(root / "services.json", max_bytes=1_048_576)
        services = manifest.get("services")
        bridge = services.get("agent-bridge") if isinstance(services, dict) else None
        if (
            manifest.get("schema_version") != 1
            or manifest.get("owner") != "v0.25-named-owned-core"
            or manifest.get("instance_id") != identity.instance_id
            or manifest.get("bridge_mode") != "owned"
            or manifest.get("bridge_id") != identity.instance_id
            or not isinstance(bridge, dict)
        ):
            raise _deny()
        marker = bridge.get("protocol_mode")
        if marker is None:
            return "v1", manifest
        if marker == "v2":
            return "v2", manifest
        raise _deny()
    except DarwinOwnedV2BootstrapError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise _deny() from None


class OwnedV2ScopedClient(CoreBridgeClientV2):
    """A signed client plus its locally verified capability selection."""

    def __init__(self, *args, agent_enabled: bool, repo_ops_enabled: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self.agent_enabled = agent_enabled
        self.repo_ops_enabled = repo_ops_enabled


def load_owned_v2_clients(
    identity: InstanceIdentity,
    config_path: Path,
    *,
    home: Path | None = None,
    timeout_seconds: float = 5,
) -> dict[str, OwnedV2ScopedClient]:
    """Load exact per-alias clients; reject until this client supports task.list.

    No fallback client or credential repair is attempted. The method capability
    check intentionally occurs after local identity/scope validation so missing,
    stale and foreign bindings remain independently testable.
    """
    try:
        if (
            not isinstance(identity, InstanceIdentity)
            or identity.instance_id == DEFAULT_INSTANCE_ID
            or identity.bridge_mode != "owned"
            or identity.bridge_id != identity.instance_id
        ):
            raise _deny()
        root = instance_service_home(identity, home=home)
        binding_path = root / _FILENAME
        document = _read_private_json(binding_path, max_bytes=_MAX_BYTES)
        resources = named_owned_bridge_resources(identity, home=home)
        if (
            not isinstance(document, dict)
            or set(document)
            != {
                "schema_version",
                "owner",
                "instance_id",
                "core_id",
                "bridge_id",
                "bridge_config_path",
                "socket_path",
                "workdirs",
            }
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["owner"] != "v0.26-named-owned-core-v2"
            or document["instance_id"] != identity.instance_id
            or document["core_id"] != identity.instance_id
            or document["bridge_id"] != identity.instance_id
            or document["bridge_config_path"] != str(resources.config_path)
            or document["socket_path"] != str(resources.socket_path)
            or not isinstance(document["workdirs"], list)
            or not 1 <= len(document["workdirs"]) <= 16
        ):
            raise _deny()

        bridge = _read_private_json(resources.config_path, max_bytes=1_048_576)
        if (
            not isinstance(bridge, dict)
            or bridge.get("protocol_mode") != "v2"
            or bridge.get("bridge_id") != identity.instance_id
            or bridge.get("socket_path") != str(resources.socket_path)
        ):
            raise _deny()

        core_config_path = Path(config_path).expanduser()
        _check_path(core_config_path, kind="owned Core TOML")
        core_config_path = core_config_path.resolve(strict=True)
        manifest = _read_private_json(root / "services.json", max_bytes=1_048_576)
        services = manifest.get("services")
        core = manifest.get("core")
        core_service = services.get("http-origin") if isinstance(services, dict) else None
        bridge_service = services.get("agent-bridge") if isinstance(services, dict) else None
        expected_core_args = [
            "-m",
            "serverfs_mcp.cli",
            "serve",
            "--transport",
            "streamable-http",
            "--config",
            str(core_config_path),
            "--http-port",
            str(core.get("port")) if isinstance(core, dict) else "",
            "--macos-instance",
            identity.instance_id,
            "--macos-agent-protocol",
            "v2",
        ]
        if (
            set(manifest)
            != {
                "schema_version",
                "owner",
                "instance_id",
                "bridge_mode",
                "bridge_id",
                "core",
                "services",
            }
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1
            or manifest.get("owner") != "v0.25-named-owned-core"
            or manifest.get("instance_id") != identity.instance_id
            or manifest.get("bridge_mode") != "owned"
            or manifest.get("bridge_id") != identity.instance_id
            or owned_protocol_marker(identity, home=home)[0] != "v2"
            or not isinstance(core, dict)
            or set(core) != {"config", "sha256", "port", "hostname"}
            or core.get("config") != str(core_config_path)
            or core.get("sha256") != hashlib.sha256(core_config_path.read_bytes()).hexdigest()
            or type(core.get("port")) is not int
            or not 1 <= core["port"] <= 65535
            or any(key != "agent-bridge" and not _service_supported(key) for key in services)
            or not isinstance(core_service, dict)
            or core_service.get("arguments") != expected_core_args
            or core_service.get("environment_file") != str(root / "http-origin-environment.json")
            or core_service.get("origin_owner") != "independent-core"
            or not isinstance(bridge_service, dict)
            or bridge_service.get("protocol_mode") != "v2"
            or bridge_service.get("executable") != core_service.get("executable")
            or bridge_service.get("bridge_config") != str(resources.config_path)
            or bridge_service.get("environment_file") is not None
            or bridge_service.get("working_directory") != str(root / "data")
            or bridge_service.get("origin_owner") != "independent-bridge"
            or bridge_service.get("arguments")
            != [
                "-B",
                "-m",
                "serverfs_mcp.darwin_owned_bridge_supervisor",
                "--instance",
                identity.instance_id,
            ]
        ):
            raise _deny()
        _verify_cloudflare_spec(root, manifest, core["port"])
        _verify_openai_spec(root, manifest, core["port"])
        environment = _read_private_json(root / "http-origin-environment.json", max_bytes=16_384)
        if (
            environment.get("SERVERFS_DATA_HOME") != str(root / "data")
            or environment.get("SERVERFS_FILE_INGRESS_ENABLED") != "false"
        ):
            raise _deny()

        workdirs, settings = load_native_config(core_config_path)
        if not settings.bridge_required:
            raise _deny()
        enabled = {
            item.alias: item
            for item in workdirs
            if item.agent_mode != "disabled" or item.policy.repo_ops_enabled
        }
        entries = document["workdirs"]
        if len(enabled) != len(entries):
            raise _deny()
        clients: dict[str, OwnedV2ScopedClient] = {}
        for item in entries:
            if not isinstance(item, dict) or set(item) != {
                "alias",
                "path",
                "key_id",
                "secret_hex",
                "policy_revision",
            }:
                raise _deny()
            alias = item["alias"]
            workdir = enabled.get(alias) if isinstance(alias, str) else None
            if (
                workdir is None
                or alias in clients
                or not _ALIAS.fullmatch(alias)
                or not isinstance(item["path"], str)
                or Path(item["path"]).resolve(strict=True)
                != Path(workdir.root).resolve(strict=True)
                or not isinstance(item["key_id"], str)
                or not _ID.fullmatch(item["key_id"])
                or not isinstance(item["secret_hex"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["secret_hex"])
                or not isinstance(item["policy_revision"], str)
                or not _DIGEST.fullmatch(item["policy_revision"])
            ):
                raise _deny()
            clients[alias] = OwnedV2ScopedClient(
                resources.socket_path,
                agent_enabled=workdir.agent_mode != "disabled",
                repo_ops_enabled=workdir.policy.repo_ops_enabled,
                bridge_id=identity.instance_id,
                core_id=identity.instance_id,
                key_id=item["key_id"],
                secret=bytes.fromhex(item["secret_hex"]),
                workdir_alias=alias,
                policy_revision=item["policy_revision"],
                timeout_seconds=timeout_seconds,
            )

        if "task.list" not in core_bridge_client_v2._METHODS:
            raise _deny()
        return clients
    except DarwinOwnedV2BootstrapError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        raise _deny() from None

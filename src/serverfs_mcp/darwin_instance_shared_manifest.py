"""Source-only verifier for a named macOS Core using an owner-managed Bridge.

This contract is deliberately not wired to launchd or Core startup. A manifest
alone never authenticates a Bridge; the later bootstrap must prove live IPC.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import darwin_service
from .darwin_instance_runtime import _check_dir, _check_path, _check_private_file
from .darwin_instance_shared_binding import DarwinSharedBindingError
from .darwin_instances import instance_service_home
from .instance_identity import InstanceIdentity
from .native_config import load_native_config

OWNER = "v0.25-named-shared-core"
INSTANCE_ENV = "SERVERFS_MACOS_INSTANCE_ID"
_MAX_BYTES = 16 * 1024
_HTTP_AUTH_KEYS = frozenset(
    {
        "SERVERFS_HTTP_API_KEY",
        "SERVERFS_HTTP_ALLOWED_HOSTS",
        "SERVERFS_OAUTH_ENABLED",
        "SERVERFS_OAUTH_RESOURCE_URL",
        "SERVERFS_OAUTH_ISSUER_URL",
        "SERVERFS_OAUTH_REQUIRED_SCOPES",
        "SERVERFS_OAUTH_INTROSPECTION_URL",
        "SERVERFS_OAUTH_INTROSPECTION_SECRET",
        "SERVERFS_OAUTH_INTROSPECTION_TIMEOUT_SECONDS",
    }
)


class DarwinSharedManifestError(ValueError):
    """A named shared-Core source manifest cannot be verified safely."""


def _deny(
    message: str = "named shared Core manifest is invalid or unsafe",
) -> DarwinSharedManifestError:
    return DarwinSharedManifestError(message)


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise _deny()
        result[key] = value
    return result


def _read_json(path: Path, *, max_bytes: int = _MAX_BYTES) -> dict[str, Any]:
    _check_private_file(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size <= 0
            or info.st_size > max_bytes
        ):
            raise _deny()
        chunks = bytearray()
        while len(chunks) <= max_bytes:
            chunk = os.read(fd, min(4096, max_bytes + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) != info.st_size or len(chunks) > max_bytes:
            raise _deny()
        raw = bytes(chunks)
    finally:
        os.close(fd)
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise _deny()
    return value


def _resolve_manifest(
    identity: InstanceIdentity,
    config_path: Path,
    port: int,
    *,
    home: Path | None,
    environ: Mapping[str, str] | None,
    expected_executable: Path | None,
) -> tuple[str | tuple[str, ...], Path]:
    if (
        not isinstance(identity, InstanceIdentity)
        or identity.instance_id == "default"
        or identity.bridge_mode != "shared"
        or not identity.bridge_id
    ):
        raise _deny("named shared Bridge identity required")
    if type(port) is not int or not 1 <= port <= 65535:
        raise _deny()
    root = instance_service_home(identity, home=home)
    _check_dir(root, private=True)
    root_info = root.stat(follow_symlinks=False)
    if stat.S_IMODE(root_info.st_mode) != 0o700:
        raise _deny()
    doc = _read_json(root / "services.json")
    services_doc = doc.get("services")
    if (
        set(doc)
        != {
            "schema_version",
            "owner",
            "instance_id",
            "bridge_mode",
            "bridge_id",
            "core",
            "services",
        }
        or type(doc.get("schema_version")) is not int
        or doc["schema_version"] != 1
        or doc.get("owner") != OWNER
        or doc.get("instance_id") != identity.instance_id
        or doc.get("bridge_mode") != "shared"
        or doc.get("bridge_id") != identity.bridge_id
        or not isinstance(services_doc, dict)
        or set(services_doc) != {"http-origin"}
    ):
        raise _deny("named shared Core manifest identity or schema mismatch")
    core = doc.get("core")
    if (
        not isinstance(core, dict)
        or set(core) != {"config", "sha256", "port", "hostname"}
        or type(core.get("port")) is not int
        or core["port"] != port
        or not isinstance(core.get("hostname"), str)
        or not core["hostname"]
    ):
        raise _deny()
    raw_config = Path(config_path).expanduser()
    _check_path(raw_config, kind="shared Core TOML")
    resolved = raw_config.resolve(strict=True)
    config_bytes = resolved.read_bytes()
    if (
        core.get("config") != str(resolved)
        or core.get("sha256") != hashlib.sha256(config_bytes).hexdigest()
    ):
        raise _deny("shared Core TOML location or digest drift")
    workdirs, settings = load_native_config(resolved)
    binding_doc = _read_json(root / "shared-bridge-binding.json")
    if binding_doc.get("schema_version") == 1:
        aliases = (binding_doc.get("workdir_alias"),)
    elif binding_doc.get("schema_version") == 2:
        entries = binding_doc.get("workdirs")
        if not isinstance(entries, list) or not 2 <= len(entries) <= 16:
            raise _deny("shared Core multi-alias binding is invalid")
        aliases = tuple(item.get("alias") if isinstance(item, dict) else None for item in entries)
    else:
        raise _deny("shared Core binding schema is unsupported")
    agent_workdirs = [item for item in workdirs if item.agent_mode != "disabled"]
    if (
        len(agent_workdirs) != len(aliases)
        or len(set(aliases)) != len(aliases)
        or {item.alias for item in agent_workdirs} != set(aliases)
        or not settings.bridge_required
        or settings.repo_ops_enabled
        or any(item.policy.repo_ops_enabled for item in workdirs)
    ):
        raise _deny("shared Core Agent workdirs do not match owner binding")
    resolved_workdirs: dict[str, Path] = {}
    for workdir in agent_workdirs:
        workdir_path = Path(workdir.root).expanduser()
        _check_path(workdir_path, kind="shared Core workdir")
        resolved_workdir = workdir_path.resolve(strict=True)
        if (
            not resolved_workdir.is_dir()
            or root == resolved_workdir
            or root in resolved_workdir.parents
            or resolved_workdir in root.parents
        ):
            raise _deny("shared Core workdir is unsafe")
        resolved_workdirs[workdir.alias] = resolved_workdir

    service = services_doc.get("http-origin")
    if not isinstance(service, dict) or set(service) != {
        "executable",
        "arguments",
        "working_directory",
        "environment_file",
        "stdout_log",
        "stderr_log",
        "origin_owner",
    }:
        raise _deny()
    expected_args = [
        "-B",
        "-m",
        "serverfs_mcp.cli",
        "serve",
        "--transport",
        "streamable-http",
        "--config",
        str(resolved),
        "--http-port",
        str(port),
        "--macos-instance",
        identity.instance_id,
        "--macos-shared-bridge",
        identity.bridge_id,
    ]
    executable = service.get("executable")
    # Runtime always pins to the running interpreter. The inactive staging
    # checker may instead pin the explicitly validated future interpreter.
    trusted_executable = (
        str(Path(sys.executable).absolute())
        if expected_executable is None
        else str(expected_executable)
    )
    logs = root / "logs"
    if (
        not isinstance(executable, str)
        or executable != trusted_executable
        or not Path(executable).is_absolute()
        or not Path(executable).is_file()
        or not os.access(executable, os.X_OK)
        or service.get("arguments") != expected_args
        or service.get("working_directory") != str(resolved.parent)
        or service.get("environment_file") != str(root / "http-origin-environment.json")
        or service.get("stdout_log") != str(logs / "core.stdout.log")
        or service.get("stderr_log") != str(logs / "core.stderr.log")
        or service.get("origin_owner") != "independent-core"
    ):
        raise _deny("shared Core launch identity is invalid")
    snapshot = _read_json(root / "http-origin-environment.json")
    if (
        set(snapshot)
        - (_HTTP_AUTH_KEYS | {INSTANCE_ENV, "SERVERFS_DATA_HOME", "SERVERFS_FILE_INGRESS_ENABLED"})
        or any(not isinstance(value, str) for value in snapshot.values())
        or snapshot.get(INSTANCE_ENV) != identity.instance_id
        or snapshot.get("SERVERFS_DATA_HOME") != str(root / "data")
        or snapshot.get("SERVERFS_FILE_INGRESS_ENABLED") != "false"
    ):
        raise _deny("shared Core environment snapshot is not isolated")
    auth_snapshot = {
        key: value
        for key, value in snapshot.items()
        if key not in {INSTANCE_ENV, "SERVERFS_DATA_HOME", "SERVERFS_FILE_INGRESS_ENABLED"}
    }
    darwin_service._validated_http_origin_environment(
        auth_snapshot, f"{core['hostname']},127.0.0.1:{port}"
    )
    binding_path = root / "shared-bridge-binding.json"
    binding_doc = _read_json(binding_path)
    socket_raw = binding_doc.get("socket_path")
    if not isinstance(socket_raw, str):
        raise _deny()
    from .darwin_shared_binding_private import load_shared_core_binding

    binding = load_shared_core_binding(
        identity=identity,
        trusted_core_home=root,
        binding_path=binding_path,
        expected_socket_path=Path(socket_raw),
    )
    grants = binding.grants or (binding.grant,)
    if len(grants) != len(resolved_workdirs) or {
        grant.workdir_alias: grant.workdir_path.resolve(strict=True) for grant in grants
    } != resolved_workdirs:
        raise _deny()
    env = os.environ if environ is None else environ
    if (
        env.get(INSTANCE_ENV, identity.instance_id) != identity.instance_id
        or env.get("SERVERFS_DATA_HOME", str(root / "data")) != str(root / "data")
        or env.get("SERVERFS_FILE_INGRESS_ENABLED", "false") != "false"
    ):
        raise _deny("shared Core environment identity disagrees")
    selected = tuple(sorted(resolved_workdirs))
    return (selected[0] if len(selected) == 1 else selected), binding.socket_path


def resolve_shared_core_manifest(
    identity: InstanceIdentity,
    config_path: Path,
    port: int,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    expected_executable: Path | None = None,
) -> tuple[str | tuple[str, ...], Path]:
    """Verify source-only v1 evidence and return (workdir alias, expected socket).

    No service is started and no live Bridge authentication is attempted here.
    """
    try:
        if expected_executable is not None and not isinstance(expected_executable, Path):
            raise _deny()
        return _resolve_manifest(
            identity,
            config_path,
            port,
            home=home,
            environ=environ,
            expected_executable=expected_executable,
        )
    except DarwinSharedManifestError:
        raise
    except (
        darwin_service.DarwinServiceError,
        DarwinSharedBindingError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
    ):
        raise _deny() from None

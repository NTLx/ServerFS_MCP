"""Inactive, atomic staging of an explicitly owner-provisioned macOS Shared Core.

The Bridge owner supplies a private binding; this code neither creates grants
nor registers/controls Bridge or Core services.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from . import darwin_service
from .darwin_instance_runtime import (
    _assert_available_port,
    _check_dir,
    _check_path,
    _new_private_file,
)
from .darwin_instance_shared_manifest import resolve_shared_core_manifest
from .darwin_instances import (
    APP_NAME,
    _validate_shared_core_python,
    _verify_source_app,
    instance_service_home,
    stage_instance_app,
)
from .darwin_shared_runtime import (
    DarwinSharedRuntimeError,
    verify_managed_shared_core_python,
)
from .instance_identity import InstanceIdentity
from .native_config import load_native_config

_MAX_BINDING_BYTES = 16 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024


class DarwinSharedStageError(ValueError):
    """Unsafe or incomplete named Shared Core staging request."""


def _probe_core_python_runtime(python: Path, config_parent: Path) -> None:
    """Fail closed if the exact canonical interpreter lacks the actual MCP runtime.

    Pinning a uv-managed base Python, rather than its virtualenv entrypoint,
    loses the venv's site-packages even if the binary is trusted and signed.
    Probe without inherited app credentials, package search overrides or
    user site-packages, before staging writes any state.
    """
    code = "import serverfs_mcp.cli; import mcp; import httpx2"
    try:
        completed = subprocess.run(
            [str(python), "-I", "-B", "-c", code],
            cwd=config_parent,
            env={
                "PATH": os.defpath,
                "HOME": str(Path.home()),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise DarwinSharedStageError(
            "shared Core Python cannot load the isolated ServerFS runtime"
        ) from None
    if completed.returncode != 0:
        raise DarwinSharedStageError("shared Core Python cannot load the isolated ServerFS runtime")


def _read_private_binding(path: Path, forbidden: tuple[Path, ...]) -> bytes:
    try:
        validated = darwin_service._secure_input_file(path, "Shared Core binding")
        darwin_service._validate_private_location(validated, forbidden, "Shared Core binding")
        fd = os.open(
            validated,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or not 2 <= info.st_size <= _MAX_BINDING_BYTES
            ):
                raise DarwinSharedStageError("shared Core binding is invalid")
            payload = os.read(fd, _MAX_BINDING_BYTES + 1)
            if len(payload) != info.st_size:
                raise DarwinSharedStageError("shared Core binding is invalid")
            return payload
        finally:
            os.close(fd)
    except (OSError, darwin_service.DarwinServiceError):
        raise DarwinSharedStageError("shared Core binding is invalid or unsafe") from None


def stage_shared_core(
    identity: InstanceIdentity,
    config_path: Path,
    env_file: Path,
    binding_file: Path,
    port: int,
    hostname: str,
    template_app: Path,
    core_python: Path,
    *,
    home: Path | None = None,
) -> dict[str, object]:
    """Build a new signed, unregistered Core; prove all persisted inputs match.

    The owner Bridge need not yet be running. Live authorization is mandatory
    at later shared-start and again during Core startup, never at install time.
    """
    if (
        not isinstance(identity, InstanceIdentity)
        or identity.instance_id == "default"
        or identity.bridge_mode != "shared"
        or identity.bridge_id is None
    ):
        raise DarwinSharedStageError("named shared Core identity required")
    if type(port) is not int or not 1 <= port <= 65535:
        raise DarwinSharedStageError("invalid shared Core port")
    if (
        not isinstance(hostname, str)
        or len(hostname) > 253
        or not all(
            1 <= len(part) <= 63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", part)
            for part in hostname.split(".")
        )
    ):
        raise DarwinSharedStageError("invalid shared Core hostname")

    base = (Path.home() if home is None else Path(home)) / "Library/Application Support/ServerFS"
    instances = base / "instances"
    root = instance_service_home(identity, home=home)
    _check_dir(base)
    if instances.exists() or instances.is_symlink():
        _check_dir(instances, private=True)
    _check_path(root, kind="named shared Core state")
    if root.exists() or root.is_symlink():
        raise DarwinSharedStageError("named shared Core already exists")
    _assert_available_port(base, identity.instance_id, port)

    python = _validate_shared_core_python(Path(core_python))
    config_path = Path(config_path).expanduser()
    _check_path(config_path, kind="shared Core TOML")
    try:
        config = config_path.resolve(strict=True)
        info = config.stat(follow_symlinks=False)
        if (
            config.suffix.lower() != ".toml"
            or not stat.S_ISREG(info.st_mode)
            or not 1 <= info.st_size <= _MAX_CONFIG_BYTES
        ):
            raise DarwinSharedStageError("shared Core TOML is invalid")
        config_bytes = config.read_bytes()
        if len(config_bytes) != info.st_size:
            raise DarwinSharedStageError("shared Core TOML changed during staging")
        workdirs, settings = load_native_config(config)
    except (OSError, ValueError, TypeError, UnicodeError):
        raise DarwinSharedStageError("shared Core TOML is invalid") from None
    enabled = [workdir for workdir in workdirs if workdir.agent_mode != "disabled"]
    if (
        not 1 <= len(enabled) <= 16
        or not settings.bridge_required
        or settings.repo_ops_enabled
        or any(workdir.policy.repo_ops_enabled for workdir in workdirs)
    ):
        raise DarwinSharedStageError("shared Core requires 1–16 Agent workdirs and no Repo Ops")
    workdir_roots: list[Path] = []
    for workdir in workdirs:
        candidate_path = Path(workdir.root).expanduser()
        _check_path(candidate_path, kind="shared Core workdir")
        candidate = candidate_path.resolve(strict=True)
        if (
            not candidate.is_dir()
            or root == candidate
            or root in candidate.parents
            or candidate in root.parents
        ):
            raise DarwinSharedStageError("shared Core workdir is unsafe")
        workdir_roots.append(candidate)

    try:
        forbidden = (Path(__file__).resolve().parents[2], *workdir_roots, root)
        env_path = darwin_service._secure_input_file(
            Path(env_file).expanduser(), "Shared Core environment"
        )
        darwin_service._validate_private_location(env_path, forbidden, "Shared Core environment")
        env = darwin_service._validated_http_origin_environment(
            darwin_service._read_env_file(env_path),
            f"{hostname},127.0.0.1:{port}",
        )
        binding = _read_private_binding(Path(binding_file).expanduser(), forbidden)
    except darwin_service.DarwinServiceError:
        raise DarwinSharedStageError("shared Core inputs are invalid or unsafe") from None
    _verify_source_app(Path(template_app))
    try:
        managed_runtime_verified = verify_managed_shared_core_python(python)
    except DarwinSharedRuntimeError:
        raise DarwinSharedStageError("managed Shared Core runtime integrity check failed") from None
    if not managed_runtime_verified:
        # Non-managed development runtimes retain the original import gate;
        # they are not treated as having verified offline wheel provenance.
        _probe_core_python_runtime(python, config.parent)

    snapshot = {
        **env,
        "SERVERFS_MACOS_INSTANCE_ID": identity.instance_id,
        "SERVERFS_DATA_HOME": str(root / "data"),
        "SERVERFS_FILE_INGRESS_ENABLED": "false",
    }
    service = {
        "executable": str(python),
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
            str(port),
            "--macos-instance",
            identity.instance_id,
            "--macos-shared-bridge",
            identity.bridge_id,
        ],
        "working_directory": str(config.parent),
        "environment_file": str(root / "http-origin-environment.json"),
        "stdout_log": str(root / "logs/core.stdout.log"),
        "stderr_log": str(root / "logs/core.stderr.log"),
        "origin_owner": "independent-core",
    }
    manifest = {
        "schema_version": 1,
        "owner": "v0.25-named-shared-core",
        "instance_id": identity.instance_id,
        "bridge_mode": "shared",
        "bridge_id": identity.bridge_id,
        "core": {
            "config": str(config),
            "sha256": hashlib.sha256(config_bytes).hexdigest(),
            "port": port,
            "hostname": hostname,
        },
        "services": {"http-origin": service},
    }

    created_instances = False
    created_candidate = False
    try:
        if not instances.exists():
            instances.mkdir(mode=0o700)
            created_instances = True
        parent = instances / identity.instance_id
        parent.mkdir(mode=0o700)
        created_candidate = True
        root.mkdir(mode=0o700)
        (root / "logs").mkdir(mode=0o700)
        (root / "data").mkdir(mode=0o700)
        _new_private_file(root / "shared-bridge-binding.json", binding)
        _new_private_file(
            root / "http-origin-environment.json",
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode(),
        )
        _new_private_file(
            root / "services.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
        )
        stage_instance_app(identity, Path(template_app), root / APP_NAME, shared_core_python=python)
        alias, _socket = resolve_shared_core_manifest(
            identity,
            config,
            port,
            home=home,
            environ={
                "SERVERFS_MACOS_INSTANCE_ID": identity.instance_id,
                "SERVERFS_DATA_HOME": str(root / "data"),
                "SERVERFS_FILE_INGRESS_ENABLED": "false",
            },
            expected_executable=python,
        )
        authorized = {alias} if isinstance(alias, str) else set(alias)
        if authorized != {item.alias for item in enabled}:
            raise DarwinSharedStageError("shared Core binding workdir mismatch")
    except BaseException:
        if created_candidate:
            shutil.rmtree(instances / identity.instance_id)
        if created_instances:
            try:
                instances.rmdir()
            except OSError:
                pass
        raise

    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "shared",
        "bridge_id": identity.bridge_id,
        "core_port": port,
        "state": "staged-not-registered",
    }

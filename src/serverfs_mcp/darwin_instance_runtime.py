"""Non-default macOS file-only Core provisioning, scoped native lifecycle.

The legacy `default` installation is never addressed by this module.
Only `bridge_mode=disabled` is executable until per-instance Bridge IPC,
lease and per-Core grants have been separately accepted.

An explicit stage operation prepares an instance-owned, private manifest and
signed ServiceManagement app. Stage never registers a LaunchAgent. Individual
Core or Cloudflare jobs require an explicit, separate start operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import darwin_service
from .darwin_instances import (
    APP_NAME,
    DarwinInstanceError,
    _read_plist,
    instance_app_path,
    instance_bundle_id,
    instance_service_home,
    stage_instance_app,
)
from .instance_identity import (
    DEFAULT_INSTANCE_ID,
    InstanceIdentity,
    InstanceIdentityError,
    valid_id,
)
from .native_config import load_native_config

_MANIFEST = "services.json"
_SNAPSHOT = "http-origin-environment.json"
_ALLOWED_SERVICES = {"http-origin": "core", "cloudflare": "cloudflare", "tunnel": "openai"}
_STAGED_OWNER = "v0.25-named-file-core"
_OWNED_OWNER = "v0.25-named-owned-core"
_SHARED_OWNER = "v0.25-named-shared-core"


def _shared_collision_owner_valid(document: dict, instance_id: str) -> bool:
    """Recognize one Shared Core without granting it Connector/Bridge ownership.

    This is an inventory check, not authorization for Core startup. A malformed
    or forged Shared document must still block resource allocation.
    """
    core = document.get("core")
    services = document.get("services")
    bridge = document.get("bridge_id")
    return (
        set(document)
        == {
            "schema_version",
            "owner",
            "instance_id",
            "bridge_mode",
            "bridge_id",
            "core",
            "services",
        }
        and type(document.get("schema_version")) is int
        and document["schema_version"] == 1
        and document.get("owner") == _SHARED_OWNER
        and document.get("instance_id") == instance_id
        and document.get("bridge_mode") == "shared"
        and isinstance(bridge, str)
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", bridge) is not None
        and isinstance(core, dict)
        and set(core) == {"config", "sha256", "port", "hostname"}
        and isinstance(core.get("config"), str)
        and core["config"].startswith("/")
        and isinstance(core.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", core["sha256"]) is not None
        and type(core.get("port")) is int
        and 1 <= core["port"] <= 65535
        and isinstance(core.get("hostname"), str)
        and bool(core["hostname"])
        and isinstance(services, dict)
        and set(services) == {"http-origin"}
        and isinstance(services["http-origin"], dict)
    )


def _cloudflare_keys(services: dict) -> list[str]:
    keys = []
    for key in services:
        if key == "cloudflare":
            keys.append(key)
        elif isinstance(key, str) and key.startswith("cloudflare."):
            try:
                valid_id(key.removeprefix("cloudflare."))
            except InstanceIdentityError as exc:
                raise DarwinInstanceRuntimeError("invalid Cloudflare service key") from exc
            keys.append(key)
    return keys


def _openai_keys(services: dict) -> list[str]:
    keys = []
    for key in services:
        if key == "tunnel":
            keys.append(key)
        elif isinstance(key, str) and key.startswith("tunnel."):
            try:
                valid_id(key.removeprefix("tunnel."))
            except InstanceIdentityError as exc:
                raise DarwinInstanceRuntimeError("invalid OpenAI service key") from exc
            keys.append(key)
    return keys


def _service_supported(key: str) -> bool:
    return (
        key in _ALLOWED_SERVICES
        or (
            isinstance(key, str)
            and key.startswith("cloudflare.")
            and bool(_cloudflare_keys({key: None}))
        )
        or (isinstance(key, str) and key.startswith("tunnel.") and bool(_openai_keys({key: None})))
    )


def _verify_openai_plist(
    identity: InstanceIdentity, connector_id: str, app: Path, *, home: Path | None = None
) -> None:
    label = identity.macos_label("openai", connector_id)
    plist = app / "Contents/Library/LaunchAgents" / f"{label}.plist"
    try:
        _check_path(plist, kind="signed OpenAI LaunchAgent")
    except darwin_service.DarwinServiceError as exc:
        raise DarwinInstanceRuntimeError("signed OpenAI LaunchAgent path is unsafe") from exc
    try:
        spec = _read_plist(plist)
    except DarwinInstanceError as exc:
        raise DarwinInstanceRuntimeError("signed OpenAI LaunchAgent is unavailable") from exc
    if spec.get("Label") != label or spec.get("ProgramArguments") != [
        "serverfs-service-launcher",
        identity.instance_id,
        f"tunnel.{connector_id}",
    ]:
        raise DarwinInstanceRuntimeError("signed OpenAI LaunchAgent identity mismatch")
    _manager_path(identity, home)


def _verify_cloudflare_plist(
    identity: InstanceIdentity, connector_id: str, app: Path, *, home: Path | None = None
) -> None:
    label = identity.macos_label("cloudflare", connector_id)
    plist = app / "Contents/Library/LaunchAgents" / f"{label}.plist"
    try:
        _check_path(plist, kind="signed Cloudflare LaunchAgent")
    except darwin_service.DarwinServiceError as exc:
        raise DarwinInstanceRuntimeError("signed Cloudflare LaunchAgent path is unsafe") from exc
    try:
        spec = _read_plist(plist)
    except DarwinInstanceError as exc:
        raise DarwinInstanceRuntimeError("signed Cloudflare LaunchAgent is unavailable") from exc
    if spec.get("Label") != label or spec.get("ProgramArguments") != [
        "serverfs-service-launcher",
        identity.instance_id,
        f"cloudflare.{connector_id}",
    ]:
        raise DarwinInstanceRuntimeError("signed Cloudflare LaunchAgent identity mismatch")
    _manager_path(identity, home)


class DarwinInstanceRuntimeError(DarwinInstanceError):
    """Named macOS instance is invalid, colliding or not safe to operate."""


def _file_only(identity: InstanceIdentity) -> None:
    if identity.instance_id == DEFAULT_INSTANCE_ID:
        raise DarwinInstanceRuntimeError("legacy default services are not managed here")
    if identity.bridge_mode != "disabled":
        raise DarwinInstanceRuntimeError(
            "named owned/shared Bridge IPC and capability grants are not yet accepted"
        )


def _check_path(path: Path, *, kind: str) -> None:
    if not path.is_absolute():
        raise DarwinInstanceRuntimeError(f"{kind} path must be absolute")
    darwin_service._reject_symlink_components(path, kind)
    if path.is_symlink():
        raise DarwinInstanceRuntimeError(f"{kind} must not be a symlink")


def _check_dir(path: Path, *, private: bool = False) -> None:
    _check_path(path, kind="instance directory")
    try:
        st = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DarwinInstanceRuntimeError("instance directory is unavailable") from exc
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise DarwinInstanceRuntimeError("instance directory owner/type mismatch")
    if private and st.st_mode & 0o077:
        raise DarwinInstanceRuntimeError("instance directory must be private mode 0700")


def _check_private_file(path: Path) -> None:
    _check_path(path, kind="instance private file")
    try:
        st = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DarwinInstanceRuntimeError("instance private file is unavailable") from exc
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != os.getuid()
        or stat.S_IMODE(st.st_mode) != 0o600
    ):
        raise DarwinInstanceRuntimeError("instance private file must be current-user mode 0600")


def _new_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _load_owned(identity: InstanceIdentity, *, home: Path | None = None) -> tuple[Path, dict]:
    _file_only(identity)
    root = instance_service_home(identity, home=home)
    _check_dir(root, private=True)
    path = root / _MANIFEST
    _check_private_file(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise DarwinInstanceRuntimeError("instance manifest cannot be read") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("owner") != _STAGED_OWNER
        or document.get("instance_id") != identity.instance_id
        or document.get("bridge_mode") != "disabled"
        or document.get("bridge_id") is not None
        or not isinstance(document.get("services"), dict)
        or any(not _service_supported(key) for key in document["services"])
        or not isinstance(document["services"].get("http-origin"), dict)
    ):
        raise DarwinInstanceRuntimeError("instance manifest identity or schema mismatch")
    return root, document


def _load_connector_owner(
    identity: InstanceIdentity, *, home: Path | None = None
) -> tuple[Path, dict]:
    """Only file-only and independently owned named Cores can own Connectors."""
    if identity.bridge_mode == "disabled":
        return _load_owned(identity, home=home)
    if (
        identity.instance_id == DEFAULT_INSTANCE_ID
        or identity.bridge_mode != "owned"
        or identity.bridge_id != identity.instance_id
    ):
        raise DarwinInstanceRuntimeError(
            "named Connector requires disabled or independent owned Core"
        )
    root = instance_service_home(identity, home=home)
    _check_dir(root, private=True)
    _check_private_file(root / _MANIFEST)
    try:
        document = json.loads((root / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise DarwinInstanceRuntimeError("named owned Connector manifest cannot be read") from exc
    services = document.get("services") if isinstance(document, dict) else None
    if (
        not isinstance(services, dict)
        or document.get("schema_version") != 1
        or document.get("owner") != _OWNED_OWNER
        or document.get("instance_id") != identity.instance_id
        or document.get("bridge_mode") != "owned"
        or document.get("bridge_id") != identity.instance_id
        or not {"http-origin", "agent-bridge"} <= set(services)
        or any(
            key not in {"http-origin", "agent-bridge", "cloudflare", "tunnel"}
            and not _service_supported(key)
            for key in services
        )
    ):
        raise DarwinInstanceRuntimeError("named owned Connector manifest identity mismatch")
    return root, document


def _connector_core_details(
    identity: InstanceIdentity,
    document: dict,
    root: Path,
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> tuple[Path, int, str]:
    if identity.bridge_mode == "disabled":
        return _core_details(identity, document, root)
    from .darwin_instance_owned_binding import resolve_owned_core_binding

    metadata = document.get("core")
    if not isinstance(metadata, dict):
        raise DarwinInstanceRuntimeError("named owned Core metadata is invalid")
    config = metadata.get("config")
    port = metadata.get("port")
    hostname = metadata.get("hostname")
    if (
        not isinstance(config, str)
        or type(port) is not int
        or not isinstance(hostname, str)
        or not hostname
    ):
        raise DarwinInstanceRuntimeError("named owned Core metadata is incomplete")
    from .darwin_owned_v2_bootstrap import load_owned_v2_clients, owned_protocol_marker

    protocol, verified = owned_protocol_marker(identity, home=home)
    if verified != document:
        raise DarwinInstanceRuntimeError("named owned Connector manifest changed")
    if protocol == "v2":
        load_owned_v2_clients(identity, Path(config), home=home)
    else:
        resolve_owned_core_binding(
            identity,
            Path(config),
            port,
            home=home,
            default_bridge_config=default_bridge_config,
        )
    return Path(config), port, hostname


def _core_details(identity: InstanceIdentity, document: dict, root: Path) -> tuple[Path, int, str]:
    core = document["services"]["http-origin"]
    info = document.get("core", {})
    if not isinstance(info, dict):
        raise DarwinInstanceRuntimeError("invalid instance Core metadata")
    config_raw = info.get("config")
    hostname = info.get("hostname")
    port = info.get("port")
    digest = info.get("sha256")
    if (
        not isinstance(config_raw, str)
        or not isinstance(hostname, str)
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise DarwinInstanceRuntimeError("invalid instance Core metadata")
    config = Path(config_raw)
    _check_path(config, kind="Core TOML")
    try:
        raw = config.read_bytes()
    except OSError as exc:
        raise DarwinInstanceRuntimeError("Core TOML cannot be read") from exc
    if hashlib.sha256(raw).hexdigest() != digest:
        raise DarwinInstanceRuntimeError("Core TOML has changed since staging")
    args = core.get("arguments")
    if (
        not isinstance(args, list)
        or args
        != [
            "-m",
            "serverfs_mcp.cli",
            "serve",
            "--transport",
            "streamable-http",
            "--config",
            str(config),
            "--http-port",
            str(port),
        ]
        or core.get("origin_owner") != "independent-core"
        or core.get("environment_file") != str(root / _SNAPSHOT)
    ):
        raise DarwinInstanceRuntimeError("Core manifest executable/arguments mismatch")
    _check_private_file(root / _SNAPSHOT)
    try:
        env = json.loads((root / _SNAPSHOT).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise DarwinInstanceRuntimeError("private Core environment is invalid") from exc
    if not isinstance(env, dict):
        raise DarwinInstanceRuntimeError("invalid private Core environment")
    if (
        env.get("SERVERFS_DATA_HOME") != str(root / "data")
        or env.get("SERVERFS_FILE_INGRESS_ENABLED") != "false"
    ):
        raise DarwinInstanceRuntimeError("file-only Core environment isolation is invalid")
    auth_env = {
        key: value
        for key, value in env.items()
        if key not in {"SERVERFS_DATA_HOME", "SERVERFS_FILE_INGRESS_ENABLED"}
    }
    darwin_service._validated_http_origin_environment(auth_env, f"{hostname},127.0.0.1:{port}")
    try:
        workdirs, settings = load_native_config(config)
    except Exception as exc:
        raise DarwinInstanceRuntimeError("Core TOML is invalid") from exc
    if settings.bridge_required or any(
        w.agent_mode != "disabled" or w.policy.repo_ops_enabled for w in workdirs
    ):
        raise DarwinInstanceRuntimeError("file-only Core must not enable Agent or Repo Ops")
    if not workdirs:
        raise DarwinInstanceRuntimeError("file-only Core requires workdirs")
    for wd in workdirs:
        candidate = Path(wd.root).expanduser()
        _check_path(candidate, kind="Core workdir")
        resolved = candidate.resolve(strict=True)
        if resolved == root or resolved in root.parents or root in resolved.parents:
            raise DarwinInstanceRuntimeError("private instance state is exposed by a workdir")
    return config, port, hostname


def _read_collision_manifests(
    base: Path, candidate: str, *, allow_candidate: bool = False
) -> list[tuple[str, dict]]:
    instances = base / "instances"
    if not instances.exists() and not instances.is_symlink():
        items = ()
    else:
        _check_dir(instances, private=True)
        items = instances.iterdir()
    found: list[tuple[str, dict]] = []
    for item in items:
        _check_dir(item, private=True)
        if item.name == candidate:
            if allow_candidate:
                continue
            raise DarwinInstanceRuntimeError(
                "instance ID already occupies a private state directory"
            )
        home = item / "service-manager"
        _check_dir(home, private=True)
        path = home / _MANIFEST
        _check_private_file(path)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise DarwinInstanceRuntimeError(
                "another instance manifest cannot be inspected"
            ) from exc
        if (
            not isinstance(doc, dict)
            or doc.get("owner") not in {_STAGED_OWNER, _OWNED_OWNER, _SHARED_OWNER}
            or doc.get("instance_id") != item.name
            or (
                doc.get("owner") == _STAGED_OWNER
                and (doc.get("bridge_mode") != "disabled" or doc.get("bridge_id") is not None)
            )
            or (
                doc.get("owner") == _OWNED_OWNER
                and (doc.get("bridge_mode") != "owned" or doc.get("bridge_id") != item.name)
            )
            or (
                doc.get("owner") == _SHARED_OWNER
                and not _shared_collision_owner_valid(doc, item.name)
            )
            or not isinstance(doc.get("services"), dict)
            or any(key != "agent-bridge" and not _service_supported(key) for key in doc["services"])
            or not isinstance(doc.get("core"), dict)
            or type(doc["core"].get("port")) is not int
            or not 1 <= doc["core"]["port"] <= 65535
        ):
            raise DarwinInstanceRuntimeError("unknown named instance blocks resource allocation")
        found.append((item.name, doc))
    default_manifest = base / "service-manager" / _MANIFEST
    if default_manifest.is_file():
        _check_private_file(default_manifest)
        try:
            doc = json.loads(default_manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise DarwinInstanceRuntimeError(
                "default service manifest cannot be inspected"
            ) from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
            raise DarwinInstanceRuntimeError("default service manifest is invalid")
        core = doc["services"].get("http-origin")
        if isinstance(core, dict):
            args = core.get("arguments")
            if isinstance(args, list) and "--http-port" in args:
                try:
                    port = int(args[args.index("--http-port") + 1])
                except (IndexError, ValueError, TypeError) as exc:
                    raise DarwinInstanceRuntimeError(
                        "default Core port cannot be inspected"
                    ) from exc
                found.append(("default", {"core": {"port": port}}))
    return found


def _assert_available_port(
    base: Path, instance_id: str, port: int, *, allow_candidate: bool = False
) -> None:
    if any(
        doc["core"]["port"] == port
        for _, doc in _read_collision_manifests(base, instance_id, allow_candidate=allow_candidate)
    ):
        raise DarwinInstanceRuntimeError("Core TCP port is already claimed by another instance")
    if not darwin_service._loopback_port_available(port):
        raise DarwinInstanceRuntimeError("Core TCP port is already bound")


def _prepare_file_only(
    identity: InstanceIdentity,
    config_path: Path,
    env_file: Path,
    port: int,
    hostname: str,
    root: Path,
    *,
    owned: bool = False,
) -> tuple[dict, bytes]:
    if owned:
        if (
            identity.instance_id == DEFAULT_INSTANCE_ID
            or identity.bridge_mode != "owned"
            or identity.bridge_id != identity.instance_id
        ):
            raise DarwinInstanceRuntimeError("named owned Core requires its own Bridge identity")
    else:
        _file_only(identity)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise DarwinInstanceRuntimeError("invalid Core TCP port")
    hostname_ok = (
        isinstance(hostname, str)
        and len(hostname) <= 253
        and all(
            1 <= len(part) <= 63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", part)
            for part in hostname.split(".")
        )
    )
    if not hostname_ok:
        raise DarwinInstanceRuntimeError("invalid exact Core hostname")
    config_path = config_path.expanduser()
    _check_path(config_path, kind="Core TOML")
    if config_path.suffix.lower() != ".toml" or not config_path.is_file():
        raise DarwinInstanceRuntimeError("native Core TOML is unavailable")
    try:
        workdirs, settings = load_native_config(config_path)
        raw_config = config_path.read_bytes()
    except Exception as exc:
        raise DarwinInstanceRuntimeError("native file-only configuration is invalid") from exc
    if owned:
        if not settings.bridge_required or not any(
            w.agent_mode != "disabled" or w.policy.repo_ops_enabled for w in workdirs
        ):
            raise DarwinInstanceRuntimeError("owned Core requires Agent/Repo Ops policy")
    elif settings.bridge_required or any(
        w.agent_mode != "disabled" or w.policy.repo_ops_enabled for w in workdirs
    ):
        raise DarwinInstanceRuntimeError(
            "named Core cannot enable Agent/Repo Ops without Bridge grant"
        )
    if not workdirs:
        raise DarwinInstanceRuntimeError("named Core needs one or more workdirs")
    exposed: list[Path] = []
    for workdir in workdirs:
        candidate = Path(workdir.root).expanduser()
        _check_path(candidate, kind="workdir")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise DarwinInstanceRuntimeError("configured workdir is unavailable") from exc
        if not resolved.is_dir():
            raise DarwinInstanceRuntimeError("configured workdir must be a directory")
        if root == resolved or root in resolved.parents or resolved in root.parents:
            raise DarwinInstanceRuntimeError("private instance state must not overlap workdirs")
        exposed.append(resolved)
    env_file = env_file.expanduser()
    try:
        env_file = darwin_service._secure_input_file(env_file, "named Core env file")
        darwin_service._validate_private_location(
            env_file, (Path(__file__).resolve().parents[2], *exposed), "named Core env file"
        )
        env = darwin_service._validated_http_origin_environment(
            darwin_service._read_env_file(env_file),
            f"{hostname},127.0.0.1:{port}",
        )
    except darwin_service.DarwinServiceError as exc:
        raise DarwinInstanceRuntimeError(str(exc)) from exc
    logs = root / "logs"
    snapshot = root / _SNAPSHOT
    config = config_path.resolve(strict=True)
    spec = {
        "executable": str(Path(sys.executable).absolute()),
        "arguments": [
            "-m",
            "serverfs_mcp.cli",
            "serve",
            "--transport",
            "streamable-http",
            "--config",
            str(config),
            "--http-port",
            str(port),
            *(["--macos-instance", identity.instance_id] if owned else []),
        ],
        "environment_file": str(snapshot),
        "working_directory": str(config.parent),
        "stdout_log": str(logs / "core.stdout.log"),
        "stderr_log": str(logs / "core.stderr.log"),
        "origin_owner": "independent-core",
    }
    # Explicitly override ambient launchd data home and file-ingress settings.
    env["SERVERFS_DATA_HOME"] = str(root / "data")
    env["SERVERFS_FILE_INGRESS_ENABLED"] = "false"
    if owned:
        env["SERVERFS_MACOS_INSTANCE_ID"] = identity.instance_id
    doc = {
        "schema_version": 1,
        "owner": _OWNED_OWNER if owned else _STAGED_OWNER,
        "instance_id": identity.instance_id,
        "bridge_mode": "owned" if owned else "disabled",
        "bridge_id": identity.instance_id if owned else None,
        "core": {
            "config": str(config),
            "sha256": hashlib.sha256(raw_config).hexdigest(),
            "port": port,
            "hostname": hostname,
        },
        "services": {"http-origin": spec},
    }
    return doc, json.dumps(env, sort_keys=True, separators=(",", ":")).encode("utf-8")


def stage_file_only_instance(
    identity: InstanceIdentity,
    config_path: Path,
    env_file: Path,
    port: int,
    hostname: str,
    template_app: Path,
    *,
    connector_ids: dict[str, list[str] | tuple[str, ...]] | None = None,
    home: Path | None = None,
) -> dict[str, object]:
    """Provision only a new, inactive named Core and signed bundle; fail closed.

    No legacy service, file, application or named service is replaced.
    All new writes are under the new instance's private state root.
    """
    _file_only(identity)
    base = (Path.home() if home is None else home) / "Library/Application Support/ServerFS"
    _check_dir(base)
    instances = base / "instances"
    if instances.exists() or instances.is_symlink():
        _check_dir(instances, private=True)
    root = base / identity.macos_data_relative()
    _check_path(root, kind="instance state")
    # All validation and collision probes happen before allocating any state.
    _assert_available_port(base, identity.instance_id, port)
    document, snapshot = _prepare_file_only(identity, config_path, env_file, port, hostname, root)
    template_app = Path(template_app)
    from .darwin_instances import _verify_source_app

    _verify_source_app(template_app)
    created_instances_dir = False
    if not instances.exists():
        instances.mkdir(mode=0o700)
        created_instances_dir = True
    created_root = False
    try:
        instance_dir = instances / identity.instance_id
        instance_dir.mkdir(mode=0o700)
        created_root = True
        root.mkdir(mode=0o700)
        (root / "logs").mkdir(mode=0o700)
        (root / "data").mkdir(mode=0o700)
        _new_private_file(root / _SNAPSHOT, snapshot)
        _new_private_file(
            root / _MANIFEST,
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
        )
        stage_instance_app(
            identity,
            template_app,
            root / APP_NAME,
            **({"connector_ids": connector_ids} if connector_ids else {}),
        )
    except Exception:
        if created_root:
            shutil.rmtree(instances / identity.instance_id)
        if created_instances_dir:
            try:
                instances.rmdir()
            except OSError:
                pass
        raise
    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "disabled",
        "state": "staged-not-registered",
        "core_port": port,
    }


def _manager_path(identity: InstanceIdentity, home: Path | None = None) -> Path:
    app = instance_app_path(identity, home=home)
    _check_path(app, kind="named app bundle")
    info = _read_plist(app / "Contents/Info.plist")
    if (
        info.get("CFBundleIdentifier") != instance_bundle_id(identity)
        or info.get("ServerFSInstanceID") != identity.instance_id
    ):
        raise DarwinInstanceRuntimeError("named ServiceManager bundle identity mismatch")
    manager = app / "Contents/MacOS/serverfs-service-manager"
    if not manager.is_file() or not os.access(manager, os.X_OK):
        raise DarwinInstanceRuntimeError("named ServiceManager executable missing")
    try:
        verify = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DarwinInstanceRuntimeError(
            "named ServiceManager signature verification failed"
        ) from exc
    if verify.returncode != 0:
        raise DarwinInstanceRuntimeError("named ServiceManager signature verification failed")
    return manager


def _manager(
    identity: InstanceIdentity, operation: str, component: str, *, home: Path | None = None
) -> str:
    if operation not in {"register", "unregister", "status"} or (
        not _service_supported(component)
        and not (
            component == "agent-bridge"
            and identity.instance_id != DEFAULT_INSTANCE_ID
            and identity.bridge_mode == "owned"
            and identity.bridge_id == identity.instance_id
        )
    ):
        raise DarwinInstanceRuntimeError("invalid named service operation")
    executable = _manager_path(identity, home)
    try:
        completed = subprocess.run(
            [str(executable), operation, component],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise DarwinInstanceRuntimeError("named ServiceManager could not report state") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or completed.returncode
        or not isinstance(payload.get("services"), dict)
        or not isinstance(payload["services"].get(component), str)
    ):
        raise DarwinInstanceRuntimeError("named ServiceManager refused lifecycle operation")
    return payload["services"][component]


def instance_status(identity: InstanceIdentity, *, home: Path | None = None) -> dict[str, object]:
    root, document = _load_owned(identity, home=home)
    _, port, hostname = _core_details(identity, document, root)
    _verify_cloudflare_spec(root, document, port)
    _verify_openai_spec(root, document, port)
    statuses = {}
    pids = {}
    for component in document["services"]:
        if component.startswith("cloudflare."):
            name = component
            label = identity.macos_label("cloudflare", component.removeprefix("cloudflare."))
        elif component.startswith("tunnel."):
            name = f"openai.{component.removeprefix('tunnel.')}"
            label = identity.macos_label("openai", component.removeprefix("tunnel."))
        else:
            name = _ALLOWED_SERVICES[component]
            label = identity.macos_label(name)
        statuses[name] = _manager(identity, "status", component, home=home)
        pids[name] = darwin_service._launchd_job_pid(label)
    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "disabled",
        "core_port": port,
        "hostname": hostname,
        "components": statuses,
        "pids": pids,
    }


def _anonymous_core_401(port: int) -> bool:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=0.75) as response:
            return response.status == 401
    except urllib.error.HTTPError as exc:
        return exc.code == 401
    except (OSError, ValueError):
        return False


def _core_listener_owned_by(pid: int, port: int) -> bool:
    """Require the launchd-owned Core PID itself to hold its loopback listener."""
    if pid <= 0 or not 1 <= port <= 65535:
        return False
    try:
        probe = subprocess.run(
            [
                "lsof",
                "-nP",
                "-a",
                "-p",
                str(pid),
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-F",
                "n",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and f"n127.0.0.1:{port}" in probe.stdout.splitlines()


def _wait_component_ready(
    identity: InstanceIdentity,
    component: str,
    port: int,
    *,
    connector_id: str | None = None,
    timeout: float = 15.0,
) -> int:
    label = identity.macos_label(component, connector_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = darwin_service._launchd_job_pid(label)
        if pid is not None and (
            component != "core"
            or (_core_listener_owned_by(pid, port) and _anonymous_core_401(port))
        ):
            return pid
        time.sleep(0.2)
    raise DarwinInstanceRuntimeError("named component was registered but not ready")


def _wait_component_stopped(
    identity: InstanceIdentity,
    component: str,
    *,
    connector_id: str | None = None,
    timeout: float = 15.0,
) -> None:
    label = identity.macos_label(component, connector_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if darwin_service._launchd_job_pid(label) is None:
            return
        time.sleep(0.2)
    raise DarwinInstanceRuntimeError("named component remains running after unregister")


def _component_key(component: str) -> str:
    return {"core": "http-origin", "cloudflare": "cloudflare", "openai": "tunnel"}.get(
        component
    ) or _invalid_component()


def start_component(
    identity: InstanceIdentity,
    component: str,
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
    connector_id: str | None = None,
) -> dict[str, object]:
    if connector_id is not None:
        if component not in {"cloudflare", "openai"}:
            raise DarwinInstanceRuntimeError("connector ID is only valid for ingress components")
        try:
            valid_id(connector_id)
        except InstanceIdentityError as exc:
            raise DarwinInstanceRuntimeError("invalid connector ID") from exc
    if identity.bridge_mode == "owned" and component == "core":
        raise DarwinInstanceRuntimeError(
            "owned Core must use the supervised Bridge->Core lifecycle"
        )
    root, doc = _load_connector_owner(identity, home=home)
    _, port, _ = _connector_core_details(
        identity, doc, root, home=home, default_bridge_config=default_bridge_config
    )
    _verify_cloudflare_spec(root, doc, port)
    _verify_openai_spec(root, doc, port)
    service_id = _component_key(component)
    if connector_id is not None:
        service_id = f"{'cloudflare' if component == 'cloudflare' else 'tunnel'}.{connector_id}"
    if service_id not in doc["services"]:
        raise DarwinInstanceRuntimeError("requested component is not staged")
    if _manager(identity, "status", service_id, home=home) not in {"not-registered", "not-found"}:
        raise DarwinInstanceRuntimeError("component already registered; no implicit replacement")
    base = root.parents[2]
    if component == "core":
        _assert_available_port(base, identity.instance_id, port, allow_candidate=True)
    else:
        if _manager(identity, "status", "http-origin", home=home) != "enabled":
            raise DarwinInstanceRuntimeError("Connector requires an enabled Core")
        _wait_component_ready(identity, "core", port, timeout=3.0)
        if connector_id is not None:
            app = root / APP_NAME
            if component == "cloudflare":
                _verify_cloudflare_plist(identity, connector_id, app, home=home)
            else:
                _verify_openai_plist(identity, connector_id, app, home=home)
    state = _manager(identity, "register", service_id, home=home)
    try:
        if state != "enabled":
            raise DarwinInstanceRuntimeError(
                "named component needs user approval or is not enabled"
            )
        pid = _wait_component_ready(identity, component, port, connector_id=connector_id)
    except DarwinInstanceRuntimeError:
        try:
            removed = _manager(identity, "unregister", service_id, home=home)
            if removed not in {"not-registered", "not-found"}:
                raise DarwinInstanceRuntimeError("scoped rollback did not unregister component")
            _wait_component_stopped(identity, component, connector_id=connector_id)
        except DarwinInstanceRuntimeError as rollback_exc:
            raise DarwinInstanceRuntimeError(
                "named component start failed and scoped rollback also failed"
            ) from rollback_exc
        raise
    return {
        "instance_id": identity.instance_id,
        "component": service_id if connector_id is not None else component,
        "registration": state,
        "pid": pid,
    }


def stop_component(
    identity: InstanceIdentity,
    component: str,
    *,
    home: Path | None = None,
    connector_id: str | None = None,
) -> dict[str, object]:
    if connector_id is not None:
        if component not in {"cloudflare", "openai"}:
            raise DarwinInstanceRuntimeError("connector ID is only valid for ingress components")
        try:
            valid_id(connector_id)
        except InstanceIdentityError as exc:
            raise DarwinInstanceRuntimeError("invalid connector ID") from exc
    if identity.bridge_mode == "owned" and component == "core":
        raise DarwinInstanceRuntimeError("owned Core must stop via owned-stop after Connectors")
    _, doc = _load_connector_owner(identity, home=home)
    service_id = _component_key(component)
    if connector_id is not None:
        service_id = f"{'cloudflare' if component == 'cloudflare' else 'tunnel'}.{connector_id}"
    if service_id not in doc["services"]:
        raise DarwinInstanceRuntimeError("requested component is not staged")
    if connector_id is not None:
        root = instance_service_home(identity, home=home)
        _, port, _ = _connector_core_details(identity, doc, root, home=home)
        if component == "cloudflare":
            _verify_cloudflare_spec(root, doc, port)
            _verify_cloudflare_plist(identity, connector_id, root / APP_NAME, home=home)
        else:
            _verify_openai_spec(root, doc, port)
            _verify_openai_plist(identity, connector_id, root / APP_NAME, home=home)
    if component == "core":
        for connector in (*_cloudflare_keys(doc["services"]), *_openai_keys(doc["services"])):
            if connector in doc["services"] and _manager(
                identity, "status", connector, home=home
            ) not in {
                "not-registered",
                "not-found",
            }:
                raise DarwinInstanceRuntimeError(
                    "stop the named Connector before its Core (Cloudflare or OpenAI)"
                )
            launch_component, launch_id = (
                ("cloudflare", connector.removeprefix("cloudflare."))
                if connector.startswith("cloudflare.")
                else ("openai", connector.removeprefix("tunnel."))
                if connector.startswith("tunnel.")
                else (_ALLOWED_SERVICES[connector], None)
            )
            if (
                darwin_service._launchd_job_pid(identity.macos_label(launch_component, launch_id))
                is not None
            ):
                raise DarwinInstanceRuntimeError("Connector process is still running")
    state = _manager(identity, "unregister", service_id, home=home)
    if state not in {"not-registered", "not-found"}:
        raise DarwinInstanceRuntimeError("named component was not unregistered")
    _wait_component_stopped(identity, component, connector_id=connector_id)
    return {
        "instance_id": identity.instance_id,
        "component": service_id if connector_id is not None else component,
        "registration": state,
    }


def unstage_file_only_instance(identity: InstanceIdentity, *, home: Path | None = None) -> None:
    root, doc = _load_owned(identity, home=home)
    _, port, _ = _core_details(identity, doc, root)
    _verify_cloudflare_spec(root, doc, port)
    _verify_openai_spec(root, doc, port)
    for component in doc["services"]:
        if _manager(identity, "status", component, home=home) not in {
            "not-registered",
            "not-found",
        }:
            raise DarwinInstanceRuntimeError("all named jobs must be unregistered before unstage")
        # launchd registration may be stale; must not remove an owned bundle while a PID exists.
        if component.startswith("cloudflare."):
            label = identity.macos_label("cloudflare", component.removeprefix("cloudflare."))
        elif component.startswith("tunnel."):
            label = identity.macos_label("openai", component.removeprefix("tunnel."))
        else:
            label = identity.macos_label(_ALLOWED_SERVICES[component])
        pid = darwin_service._launchd_job_pid(label)
        if pid is not None:
            raise DarwinInstanceRuntimeError("named service process is still running")
    # Avoid erasing unknown or operator-owned files. Only remove the exact
    # staged layout when no extra artifacts exist (logs must be empty).
    expected = {_MANIFEST, _SNAPSHOT, "logs", "data", APP_NAME}
    if {item.name for item in root.iterdir()} != expected:
        raise DarwinInstanceRuntimeError("unexpected private state files; unstage refused")
    if any((root / "logs").iterdir()) or any((root / "data").iterdir()):
        raise DarwinInstanceRuntimeError("service state/logs retained; unstage refused")
    if {item.name for item in root.parent.iterdir()} != {"service-manager"}:
        raise DarwinInstanceRuntimeError("unexpected instance directory contents; unstage refused")
    shutil.rmtree(root / APP_NAME)
    (root / _MANIFEST).unlink()
    (root / _SNAPSHOT).unlink()
    (root / "logs").rmdir()
    (root / "data").rmdir()
    root.rmdir()
    parent = root.parent
    parent.rmdir()
    # Keep the shared instances/ root (and all legacy service files) intact.


def _replace_manifest(root: Path, document: dict) -> None:
    """Atomic replacement of only a manifest already owned by this named instance."""
    path = root / _MANIFEST
    _check_private_file(path)
    temp = root / f".{_MANIFEST}.{os.getpid()}.tmp"
    try:
        _new_private_file(
            temp, json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        )
        _check_private_file(path)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _verify_cloudflare_spec(root: Path, doc: dict, port: int) -> None:
    for key in _cloudflare_keys(doc["services"]):
        connector = doc["services"].get(key)
        suffix = key.removeprefix("cloudflare")
        logbase = f"cloudflare{suffix}"
        if not isinstance(connector, dict):
            raise DarwinInstanceRuntimeError("Cloudflare connector manifest is malformed")
        args = connector.get("arguments")
        if (
            connector.get("connector_owner") != "independent-connector"
            or connector.get("core_port") != port
            or not isinstance(args, list)
            or len(args) != 5
            or args[:4] != ["tunnel", "--no-autoupdate", "run", "--token-file"]
            or connector.get("environment_file") is not None
            or connector.get("stdout_log") != str(root / f"logs/{logbase}.stdout.log")
            or connector.get("stderr_log") != str(root / f"logs/{logbase}.stderr.log")
        ):
            raise DarwinInstanceRuntimeError("Cloudflare connector owner/arguments mismatch")
        token = Path(args[4])
        _check_private_file(token)
        binary = Path(connector.get("executable", ""))
        _check_path(binary, kind="cloudflared executable")
        st = binary.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o022
            or not os.access(binary, os.X_OK)
        ):
            raise DarwinInstanceRuntimeError("cloudflared executable is insecure")
        if key != "cloudflare":
            connector_id = key.removeprefix("cloudflare.")
            _verify_cloudflare_plist(
                InstanceIdentity(doc["instance_id"], doc["bridge_mode"], doc.get("bridge_id")),
                connector_id,
                root / APP_NAME,
                home=root.parents[5],
            )


def _reject_named_cloudflare_token_collision(root: Path, token: Path) -> None:
    """A named Connector cannot claim another named instance's token file.

    Compare underlying files, not textual paths: hardlinks and alternate
    spellings must not bypass per-instance credential ownership.
    The token value is never read or logged.
    """
    instances = root.parents[1]
    _check_dir(instances, private=True)
    for candidate in instances.iterdir():
        _check_dir(candidate, private=True)
        if candidate == root.parent:
            continue
        manifest = candidate / "service-manager" / _MANIFEST
        _check_private_file(manifest)
        try:
            other = json.loads(manifest.read_text(encoding="utf-8"))
            if (
                not isinstance(other, dict)
                or other.get("owner") not in {_STAGED_OWNER, _OWNED_OWNER, _SHARED_OWNER}
                or other.get("instance_id") != candidate.name
                or (
                    other.get("owner") == _STAGED_OWNER
                    and (
                        other.get("bridge_mode") != "disabled" or other.get("bridge_id") is not None
                    )
                )
                or (
                    other.get("owner") == _OWNED_OWNER
                    and (
                        other.get("bridge_mode") != "owned"
                        or other.get("bridge_id") != candidate.name
                    )
                )
                or (
                    other.get("owner") == _SHARED_OWNER
                    and not _shared_collision_owner_valid(other, candidate.name)
                )
                or not isinstance(other.get("services"), dict)
            ):
                raise ValueError("unrecognized owner")
            for connector_key in _cloudflare_keys(other["services"]):
                spec = other["services"][connector_key]
                if not isinstance(spec, dict) or not isinstance(spec.get("arguments"), list):
                    raise ValueError("invalid connector")
                args = spec["arguments"]
                if args.count("--token-file") != 1:
                    raise ValueError("ambiguous token file")
                other_path = Path(args[args.index("--token-file") + 1])
                _check_private_file(other_path)
                if os.path.samefile(other_path, token):
                    raise DarwinInstanceRuntimeError(
                        "named Cloudflare Connector token file already belongs to another instance"
                    )
                other_path = None
        except DarwinInstanceRuntimeError:
            raise
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise DarwinInstanceRuntimeError(
                "another named Cloudflare Connector ownership is unverifiable"
            ) from exc


def stage_cloudflare_connector(
    identity: InstanceIdentity,
    cloudflared: Path,
    token_file: Path,
    *,
    connector_id: str | None = None,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> dict[str, object]:
    """Stage a dedicated connector without registering it or mutating the Core."""
    root, doc = _load_connector_owner(identity, home=home)
    if connector_id is not None:
        try:
            valid_id(connector_id)
        except InstanceIdentityError as exc:
            raise DarwinInstanceRuntimeError("invalid Cloudflare Connector ID") from exc
        _verify_cloudflare_plist(identity, connector_id, root / APP_NAME, home=home)
    key = "cloudflare" if connector_id is None else f"cloudflare.{connector_id}"
    config, port, _ = _connector_core_details(
        identity, doc, root, home=home, default_bridge_config=default_bridge_config
    )
    if key in doc["services"]:
        raise DarwinInstanceRuntimeError("named Cloudflare connector already staged")
    if _manager(identity, "status", key, home=home) not in {"not-found", "not-registered"}:
        raise DarwinInstanceRuntimeError("named Cloudflare connector already registered")
    _, settings = load_native_config(config)
    if settings.bridge_required != (identity.bridge_mode == "owned"):
        raise DarwinInstanceRuntimeError("named connector Core/Bridge policy mismatch")
    workdirs, _ = load_native_config(config)
    roots = tuple(Path(w.root).expanduser().resolve(strict=True) for w in workdirs)
    try:
        token = darwin_service._secure_input_file(
            token_file.expanduser(), "named Cloudflare token file"
        )
        darwin_service._validate_private_location(
            token,
            (root, Path(__file__).resolve().parents[2], *roots),
            "named Cloudflare token file",
        )
        binary = cloudflared.expanduser()
        _check_path(binary, kind="cloudflared executable")
        st = binary.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o022
            or not os.access(binary, os.X_OK)
        ):
            raise DarwinInstanceRuntimeError("cloudflared executable must be pinned and secure")
    except darwin_service.DarwinServiceError as exc:
        raise DarwinInstanceRuntimeError(str(exc)) from exc

    # A new connector must not accidentally attach to the still-running
    # default production Tunnel's credential file.
    legacy = root.parents[2] / "service-manager" / _MANIFEST
    if legacy.is_file():
        _check_private_file(legacy)
        try:
            legacy_doc = json.loads(legacy.read_text(encoding="utf-8"))
            legacy_cf = legacy_doc.get("services", {}).get("cloudflare", {})
            legacy_args = legacy_cf.get("arguments", [])
        except (OSError, ValueError, AttributeError) as exc:
            raise DarwinInstanceRuntimeError(
                "default Connector ownership cannot be checked"
            ) from exc
        if not isinstance(legacy_args, list):
            raise DarwinInstanceRuntimeError("default Connector arguments are malformed")
        if "--token-file" in legacy_args:
            try:
                existing_token = Path(legacy_args[legacy_args.index("--token-file") + 1])
            except (IndexError, TypeError) as exc:
                raise DarwinInstanceRuntimeError("default Connector token file is invalid") from exc
            if existing_token.is_absolute():
                try:
                    if os.path.samefile(existing_token, token):
                        raise DarwinInstanceRuntimeError(
                            "named Connector cannot reuse the default production token file"
                        )
                except OSError as exc:
                    raise DarwinInstanceRuntimeError(
                        "default Connector token ownership cannot be verified"
                    ) from exc

    for existing_key in _cloudflare_keys(doc["services"]):
        existing_args = doc["services"][existing_key].get("arguments", [])
        if "--token-file" in existing_args:
            existing_token = Path(existing_args[existing_args.index("--token-file") + 1])
            if os.path.samefile(existing_token, token):
                raise DarwinInstanceRuntimeError(
                    "Cloudflare Connector token file is already staged"
                )
    _reject_named_cloudflare_token_collision(root, token)

    spec = {
        "executable": str(binary),
        "arguments": ["tunnel", "--no-autoupdate", "run", "--token-file", str(token)],
        "environment_file": None,
        "working_directory": str(config.parent),
        "stdout_log": str(root / f"logs/{key}.stdout.log"),
        "stderr_log": str(root / f"logs/{key}.stderr.log"),
        "connector_owner": "independent-connector",
        "core_port": port,
    }
    _verify_cloudflare_spec(root, {**doc, "services": {key: spec}}, port)
    doc["services"][key] = spec
    _replace_manifest(root, doc)
    return {
        "instance_id": identity.instance_id,
        "component": key,
        "state": "staged-not-registered",
    }


def unstage_cloudflare_connector(
    identity: InstanceIdentity,
    *,
    connector_id: str | None = None,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> None:
    root, doc = _load_connector_owner(identity, home=home)
    if connector_id is not None:
        try:
            valid_id(connector_id)
        except InstanceIdentityError as exc:
            raise DarwinInstanceRuntimeError("invalid Cloudflare Connector ID") from exc
        _verify_cloudflare_plist(identity, connector_id, root / APP_NAME, home=home)
    key = "cloudflare" if connector_id is None else f"cloudflare.{connector_id}"
    _, port, _ = _connector_core_details(
        identity, doc, root, home=home, default_bridge_config=default_bridge_config
    )
    if key not in doc["services"]:
        raise DarwinInstanceRuntimeError("named Cloudflare connector is not staged")
    _verify_cloudflare_spec(root, doc, port)
    if _manager(identity, "status", key, home=home) not in {"not-registered", "not-found"}:
        raise DarwinInstanceRuntimeError("Cloudflare connector must be unregistered first")
    label = identity.macos_label("cloudflare", connector_id)
    if darwin_service._launchd_job_pid(label) is not None:
        raise DarwinInstanceRuntimeError("Cloudflare connector process still exists")
    del doc["services"][key]
    _replace_manifest(root, doc)


def _invalid_component() -> str:
    raise DarwinInstanceRuntimeError("unsupported named instance component")


def _other_tunnel_ids(root: Path) -> set[str]:
    """Read identity claims without accessing another instance's credential values."""
    base = root.parents[2]
    manifests = [base / "service-manager" / _MANIFEST]
    instances = base / "instances"
    if instances.exists():
        _check_dir(instances, private=True)
        for entry in instances.iterdir():
            _check_dir(entry, private=True)
            if entry != root.parent:
                manifests.append(entry / "service-manager" / _MANIFEST)
    ids: set[str] = set()
    for path in manifests:
        if not path.exists() and not path.is_symlink():
            if path.parent == base / "service-manager":
                continue
            raise DarwinInstanceRuntimeError("another named instance has no manifest")
        _check_private_file(path)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            services = document["services"]
            spec = services.get("tunnel")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DarwinInstanceRuntimeError("another Tunnel owner cannot be inspected") from exc
        for key in _openai_keys(services):
            spec = services[key]
            if not isinstance(spec, dict) or not isinstance(spec.get("arguments"), list):
                raise DarwinInstanceRuntimeError("another Tunnel owner is malformed")
            args = spec["arguments"]
            options = ("--control-plane.tunnel-id", "--tunnel-id")
            matches = [option for option in options if args.count(option) == 1]
            if len(matches) != 1:
                raise DarwinInstanceRuntimeError("another Tunnel identity is ambiguous")
            try:
                other_id = args[args.index(matches[0]) + 1]
            except IndexError as exc:
                raise DarwinInstanceRuntimeError("another Tunnel identity is missing") from exc
            if not isinstance(other_id, str) or not other_id:
                raise DarwinInstanceRuntimeError("another Tunnel identity is invalid")
            ids.add(other_id)
    return ids


def _reject_openai_credential_reuse(root: Path, control: Path, bearer: Path) -> None:
    instances = root.parents[1]
    _check_dir(instances, private=True)
    manifests = [root / _MANIFEST]
    manifests.extend(
        entry / "service-manager" / _MANIFEST
        for entry in instances.iterdir()
        if entry != root.parent
    )
    default = root.parents[2] / "service-manager" / _MANIFEST
    if default.exists() or default.is_symlink():
        manifests.append(default)
    for manifest in manifests:
        _check_private_file(manifest)
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
            services = document["services"]
            if (
                not isinstance(services, dict)
                or document.get("schema_version") != 1
                or (
                    manifest != default
                    and (
                        document.get("owner") not in {_STAGED_OWNER, _OWNED_OWNER, _SHARED_OWNER}
                        or document.get("instance_id") != manifest.parents[1].name
                        or not isinstance(document.get("core"), dict)
                        or (
                            document.get("owner") == _SHARED_OWNER
                            and not _shared_collision_owner_valid(
                                document, manifest.parents[1].name
                            )
                        )
                        or (
                            document.get("owner") == _STAGED_OWNER
                            and (
                                document.get("bridge_mode") != "disabled"
                                or document.get("bridge_id") is not None
                            )
                        )
                        or (
                            document.get("owner") == _OWNED_OWNER
                            and (
                                document.get("bridge_mode") != "owned"
                                or document.get("bridge_id") != document.get("instance_id")
                            )
                        )
                    )
                )
            ):
                raise ValueError("invalid services")
            for key in _openai_keys(services):
                spec = services[key]
                if not isinstance(spec, dict):
                    raise ValueError("invalid connector")
                if manifest == default and key == "tunnel":
                    paths = _legacy_default_openai_credential_paths(default.parent, document, spec)
                elif spec.get("connector_owner") == "independent-openai-http":
                    paths = _openai_manifest_credential_paths(root, document, key, spec)
                else:
                    raise ValueError("unrecognized OpenAI connector owner")
                for path in paths:
                    _check_private_file(path)
                    if os.path.samefile(path, control) or os.path.samefile(path, bearer):
                        raise DarwinInstanceRuntimeError(
                            "OpenAI Connector credential files must be distinct"
                        )
        except DarwinInstanceRuntimeError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DarwinInstanceRuntimeError(
                "another OpenAI credential owner is unverifiable"
            ) from exc


def _openai_manifest_credential_paths(
    root: Path, document: dict, key: str, spec: dict
) -> tuple[Path, Path]:
    if set(spec) != {
        "executable",
        "arguments",
        "environment_file",
        "working_directory",
        "stdout_log",
        "stderr_log",
        "connector_owner",
        "core_port",
        "tunnel_id",
        "control_key_file",
        "core_bearer_file",
    }:
        raise ValueError("independent OpenAI connector schema is invalid")
    paths = tuple(Path(spec[name]) for name in ("control_key_file", "core_bearer_file"))
    if any(not path.is_absolute() for path in paths):
        raise ValueError("invalid OpenAI credential path")
    if len(paths) != 2 or os.path.samefile(*paths):
        raise DarwinInstanceRuntimeError("OpenAI Connector credential files must be distinct")
    return paths


def _legacy_default_openai_credential_paths(
    service_root: Path, document: dict, spec: dict
) -> tuple[Path, ...]:
    """Recognize the exact v0.25 legacy direct-HTTP or managed Tunnel manifest shapes."""
    if document.get("schema_version") != 1 or "owner" in document:
        raise ValueError("unrecognized legacy service manifest")
    if spec.get("connector_owner") == "independent-openai-http":
        args = spec.get("arguments")
        if (
            set(spec)
            != {
                "executable",
                "arguments",
                "environment_file",
                "working_directory",
                "stdout_log",
                "stderr_log",
                "connector_owner",
                "core_port",
            }
            or spec.get("environment_file") is not None
            or spec.get("stdout_log") != str(service_root / "logs/openai-connector.stdout.log")
            or spec.get("stderr_log") != str(service_root / "logs/openai-connector.stderr.log")
            or not isinstance(args, list)
        ):
            raise ValueError("legacy OpenAI connector shape is invalid")
        try:
            tunnel_id = args[2]
            cp_arg = args[4]
            bearer_arg = args[8]
            core_port = spec["core_port"]
            cp = Path(cp_arg.removeprefix("file:"))
            bearer = Path(bearer_arg.removeprefix("Authorization: file:"))
            from .native_tunnel import _TUNNEL_ID

            valid_tunnel_id = isinstance(tunnel_id, str) and bool(_TUNNEL_ID.fullmatch(tunnel_id))
            expected = [
                "run",
                "--control-plane.tunnel-id",
                tunnel_id,
                "--control-plane.api-key",
                f"file:{cp}",
                "--mcp.server-url",
                f"url=http://127.0.0.1:{core_port}/mcp",
                "--mcp.extra-headers",
                f"Authorization: file:{bearer}",
                "--mcp.discovery-extra-headers",
                f"Authorization: file:{bearer}",
                "--health.listen-addr",
                "127.0.0.1:0",
            ]
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("legacy OpenAI credential arguments are invalid") from exc
        if (
            not valid_tunnel_id
            or type(core_port) is not int
            or not 1 <= core_port <= 65535
            or args != expected
            or not cp.is_absolute()
            or not bearer.is_absolute()
            or not isinstance(spec.get("executable"), str)
            or not Path(spec["executable"]).is_absolute()
            or not isinstance(spec.get("working_directory"), str)
            or not Path(spec["working_directory"]).is_absolute()
        ):
            raise ValueError("legacy OpenAI credential arguments are ambiguous")
        _check_private_file(cp)
        _check_private_file(bearer)
        if os.path.samefile(cp, bearer):
            raise DarwinInstanceRuntimeError(
                "legacy OpenAI control and Core credential files must be distinct"
            )
        return cp, bearer

    args = spec.get("arguments")
    env_path = service_root / "tunnel-environment.json"
    proxy_path = service_root / "tunnel-proxy.env"
    if (
        set(spec)
        != {
            "executable",
            "arguments",
            "environment_file",
            "working_directory",
            "stdout_log",
            "stderr_log",
        }
        or spec.get("environment_file") != str(env_path)
        or spec.get("stdout_log") != str(service_root / "logs/tunnel.stdout.log")
        or spec.get("stderr_log") != str(service_root / "logs/tunnel.stderr.log")
        or not isinstance(args, list)
        or len(args) not in {9, 11}
        or args[:8]
        != [
            "-m",
            "serverfs_mcp.cli",
            "tunnel",
            "--config",
            args[4],
            "--env-file",
            str(proxy_path),
            "--tunnel-id",
        ]
        or not isinstance(args[4], str)
        or not Path(args[4]).is_absolute()
        or not isinstance(args[8], str)
    ):
        raise ValueError("legacy default Tunnel manifest shape is invalid")
    if (
        not isinstance(spec.get("executable"), str)
        or not Path(spec["executable"]).is_absolute()
        or not isinstance(spec.get("working_directory"), str)
        or not Path(spec["working_directory"]).is_absolute()
    ):
        raise ValueError("legacy default Tunnel executable/working directory is invalid")
    try:
        environment = json.loads(env_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("legacy default Tunnel environment is unavailable") from exc
    if not isinstance(environment, dict) or any(
        key not in {"CONTROL_PLANE_API_KEY", "SERVERFS_DATA_HOME"}
        and not key.startswith("SERVERFS_FILE_INGRESS_")
        for key in environment
    ):
        raise ValueError("legacy default Tunnel environment schema is invalid")
    inline_key = environment.get("CONTROL_PLANE_API_KEY")
    if inline_key is not None and (
        not isinstance(inline_key, str)
        or not inline_key.strip()
        or inline_key != inline_key.strip()
    ):
        raise ValueError("legacy default Tunnel control-plane key is invalid")
    source_paths = [env_path]
    if len(args) == 11:
        if args[9] != "--api-key-file" or not isinstance(args[10], str):
            raise ValueError("legacy default Tunnel key source is ambiguous")
        key_path = Path(args[10])
        if not key_path.is_absolute():
            raise ValueError("legacy default Tunnel key path is invalid")
        source_paths.append(key_path)
    elif "--api-key-file" in args:
        raise ValueError("legacy default Tunnel key source is ambiguous")
    if (inline_key is None) == (len(args) != 11):
        raise ValueError("legacy default Tunnel must have exactly one control-plane key source")
    return tuple(source_paths)


def _openai_credentials(root: Path, doc: dict, key: str, port: int) -> tuple[Path, Path, str]:
    spec = doc["services"][key]
    if not isinstance(spec, dict) or spec.get("connector_owner") != "independent-openai-http":
        raise DarwinInstanceRuntimeError("named OpenAI Connector owner mismatch")
    control_file = Path(spec.get("control_key_file", ""))
    bearer_file = Path(spec.get("core_bearer_file", ""))
    if not control_file.is_absolute() or not bearer_file.is_absolute():
        raise DarwinInstanceRuntimeError(
            "OpenAI Connector credentials must be separate absolute files"
        )
    _check_private_file(control_file)
    _check_private_file(bearer_file)
    if os.path.samefile(control_file, bearer_file):
        raise DarwinInstanceRuntimeError(
            "OpenAI Connector credentials must be separate absolute files"
        )
    _check_private_file(root / _SNAPSHOT)
    try:
        private_env = json.loads((root / _SNAPSHOT).read_text(encoding="utf-8"))
        control = control_file.read_text(encoding="utf-8")
        bearer = bearer_file.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeError) as exc:
        raise DarwinInstanceRuntimeError("named OpenAI credentials are unavailable") from exc
    if not isinstance(private_env, dict):
        raise DarwinInstanceRuntimeError("named Core environment is invalid")
    core_key = private_env.get("SERVERFS_HTTP_API_KEY")
    if (
        not isinstance(core_key, str)
        or not core_key.strip()
        or private_env.get("SERVERFS_OAUTH_ENABLED") == "true"
        or not control.strip()
        or control != control.strip()
        or control == core_key
        or bearer != f"Bearer {core_key}"
    ):
        raise DarwinInstanceRuntimeError("named OpenAI/Core credentials are inconsistent")
    tunnel_id = spec.get("tunnel_id")
    from .native_tunnel import _TUNNEL_ID

    if not isinstance(tunnel_id, str) or not _TUNNEL_ID.fullmatch(tunnel_id):
        raise DarwinInstanceRuntimeError("invalid named OpenAI Tunnel identifier")
    return control_file, bearer_file, tunnel_id


def _verify_openai_spec(root: Path, doc: dict, port: int) -> None:
    seen_credentials: list[Path] = []
    seen_tunnel_ids: set[str] = set()
    identity = InstanceIdentity(doc["instance_id"], doc["bridge_mode"], doc.get("bridge_id"))
    for key in _openai_keys(doc["services"]):
        spec = doc["services"].get(key)
        control, bearer, tunnel_id = _openai_credentials(root, doc, key, port)
        if tunnel_id in seen_tunnel_ids:
            raise DarwinInstanceRuntimeError("OpenAI Tunnel ID is duplicated")
        seen_tunnel_ids.add(tunnel_id)
        for credential in (control, bearer):
            if any(os.path.samefile(credential, previous) for previous in seen_credentials):
                raise DarwinInstanceRuntimeError(
                    "OpenAI Connector credential files must be distinct"
                )
            seen_credentials.append(credential)
        args = [
            "run",
            "--control-plane.tunnel-id",
            tunnel_id,
            "--control-plane.api-key",
            f"file:{control}",
            "--mcp.server-url",
            f"url=http://127.0.0.1:{port}/mcp",
            "--mcp.extra-headers",
            f"Authorization: file:{bearer}",
            "--mcp.discovery-extra-headers",
            f"Authorization: file:{bearer}",
            "--health.listen-addr",
            "127.0.0.1:0",
        ]
        suffix = key.removeprefix("tunnel")
        logbase = f"openai{suffix}"
        if (
            spec.get("core_port") != port
            or spec.get("arguments") != args
            or spec.get("environment_file") is not None
            or spec.get("stdout_log") != str(root / f"logs/{logbase}.stdout.log")
            or spec.get("stderr_log") != str(root / f"logs/{logbase}.stderr.log")
        ):
            raise DarwinInstanceRuntimeError("named OpenAI Connector arguments are invalid")
        binary = Path(spec.get("executable", ""))
        _check_path(binary, kind="OpenAI tunnel-client executable")
        try:
            st = binary.stat(follow_symlinks=False)
        except OSError as exc:
            raise DarwinInstanceRuntimeError("OpenAI tunnel-client is unavailable") from exc
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o022
            or not os.access(binary, os.X_OK)
        ):
            raise DarwinInstanceRuntimeError("OpenAI tunnel-client is insecure")
        if key != "tunnel":
            _verify_openai_plist(
                identity, key.removeprefix("tunnel."), root / APP_NAME, home=root.parents[5]
            )


def stage_openai_http_connector(
    identity: InstanceIdentity,
    tunnel_client: Path,
    tunnel_id: str,
    control_plane_key_file: Path,
    core_bearer_file: Path,
    *,
    connector_id: str | None = None,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> dict[str, object]:
    """Stage a separate OpenAI HTTP ingress to a named disabled or owned Core."""
    root, doc = _load_connector_owner(identity, home=home)
    if connector_id is not None:
        try:
            valid_id(connector_id)
        except InstanceIdentityError as exc:
            raise DarwinInstanceRuntimeError("invalid OpenAI Connector ID") from exc
        _verify_openai_plist(identity, connector_id, root / APP_NAME, home=home)
    key = "tunnel" if connector_id is None else f"tunnel.{connector_id}"
    config, port, _hostname = _connector_core_details(
        identity, doc, root, home=home, default_bridge_config=default_bridge_config
    )
    if key in doc["services"]:
        raise DarwinInstanceRuntimeError("named OpenAI Connector is already staged")
    from .native_tunnel import _TUNNEL_ID

    if not isinstance(tunnel_id, str) or not _TUNNEL_ID.fullmatch(tunnel_id):
        raise DarwinInstanceRuntimeError("invalid OpenAI Tunnel ID")
    if tunnel_id in _other_tunnel_ids(root) or any(
        tunnel_id == doc["services"][existing].get("tunnel_id")
        for existing in _openai_keys(doc["services"])
        if isinstance(doc["services"][existing], dict)
    ):
        raise DarwinInstanceRuntimeError("OpenAI Tunnel ID is already claimed by another instance")
    if _manager(identity, "status", key, home=home) not in {"not-registered", "not-found"}:
        raise DarwinInstanceRuntimeError("named OpenAI Connector is already registered")
    workdirs, native_settings = load_native_config(config)
    if native_settings.bridge_required != (identity.bridge_mode == "owned"):
        raise DarwinInstanceRuntimeError("named OpenAI Connector/Core policy mismatch")
    exposed = tuple(Path(w.root).expanduser().resolve(strict=True) for w in workdirs)
    candidate = Path(tunnel_client).expanduser()
    _check_path(candidate, kind="OpenAI tunnel-client executable")
    try:
        st = candidate.stat(follow_symlinks=False)
    except OSError as exc:
        raise DarwinInstanceRuntimeError("OpenAI tunnel-client is unavailable") from exc
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != os.getuid()
        or st.st_mode & 0o022
        or not os.access(candidate, os.X_OK)
    ):
        raise DarwinInstanceRuntimeError("OpenAI tunnel-client must be pinned and secure")
    try:
        control = darwin_service._secure_input_file(
            control_plane_key_file.expanduser(), "named OpenAI control-plane key"
        )
        bearer = darwin_service._secure_input_file(
            core_bearer_file.expanduser(), "named OpenAI Core bearer header"
        )
        for cred in (control, bearer):
            darwin_service._validate_private_location(
                cred,
                (root, Path(__file__).resolve().parents[2], *exposed),
                "named OpenAI credential file",
            )
    except darwin_service.DarwinServiceError as exc:
        raise DarwinInstanceRuntimeError(str(exc)) from exc
    if os.path.samefile(control, bearer):
        raise DarwinInstanceRuntimeError("OpenAI/Core credentials must use separate files")
    _reject_openai_credential_reuse(root, control, bearer)
    spec = {
        "executable": str(candidate),
        "arguments": [
            "run",
            "--control-plane.tunnel-id",
            tunnel_id,
            "--control-plane.api-key",
            f"file:{control}",
            "--mcp.server-url",
            f"url=http://127.0.0.1:{port}/mcp",
            "--mcp.extra-headers",
            f"Authorization: file:{bearer}",
            "--mcp.discovery-extra-headers",
            f"Authorization: file:{bearer}",
            "--health.listen-addr",
            "127.0.0.1:0",
        ],
        "environment_file": None,
        "working_directory": str(config.parent),
        "stdout_log": str(root / f"logs/openai{key.removeprefix('tunnel')}.stdout.log"),
        "stderr_log": str(root / f"logs/openai{key.removeprefix('tunnel')}.stderr.log"),
        "connector_owner": "independent-openai-http",
        "core_port": port,
        "tunnel_id": tunnel_id,
        "control_key_file": str(control),
        "core_bearer_file": str(bearer),
    }
    trial = {**doc, "services": {**doc["services"], key: spec}}
    _verify_openai_spec(root, trial, port)
    _replace_manifest(root, trial)
    return {
        "instance_id": identity.instance_id,
        "component": "openai" if connector_id is None else key,
        "state": "staged-not-registered",
    }


def unstage_openai_http_connector(
    identity: InstanceIdentity,
    *,
    connector_id: str | None = None,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
) -> None:
    root, doc = _load_connector_owner(identity, home=home)
    if connector_id is not None:
        try:
            valid_id(connector_id)
        except InstanceIdentityError as exc:
            raise DarwinInstanceRuntimeError("invalid OpenAI Connector ID") from exc
        _verify_openai_plist(identity, connector_id, root / APP_NAME, home=home)
    key = "tunnel" if connector_id is None else f"tunnel.{connector_id}"
    _, port, _ = _connector_core_details(
        identity, doc, root, home=home, default_bridge_config=default_bridge_config
    )
    if key not in doc["services"]:
        raise DarwinInstanceRuntimeError("named OpenAI Connector is not staged")
    _verify_openai_spec(root, doc, port)
    if _manager(identity, "status", key, home=home) not in {"not-registered", "not-found"}:
        raise DarwinInstanceRuntimeError("named OpenAI Connector must be unregistered first")
    if darwin_service._launchd_job_pid(identity.macos_label("openai", connector_id)) is not None:
        raise DarwinInstanceRuntimeError("named OpenAI Connector still has a live process")
    del doc["services"][key]
    _replace_manifest(root, doc)

"""Apple-native persistent service lifecycle for ServerFS on macOS (v0.13.2).

Registration and authorization are owned by ``SMAppService`` through the tiny
``ServerFSServiceManager.app`` companion.  Python owns product configuration,
private manifests and the one-time v0.13.1 legacy hand-over.
"""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .native_tunnel import parse_env_value

APP_NAME = "ServerFSServiceManager.app"
APP_BUNDLE_ID = "com.ntlx.serverfs.service-manager"
APP_VERSION = "0.13.2"
TUNNEL_SERVICE = "tunnel"
BRIDGE_SERVICE = "agent-bridge"
HTTP_ORIGIN_SERVICE = "http-origin"
CLOUDFLARE_SERVICE = "cloudflare"
ALL_SERVICES = (TUNNEL_SERVICE, BRIDGE_SERVICE)
_CLOUDFLARE_SERVICES = (HTTP_ORIGIN_SERVICE, CLOUDFLARE_SERVICE)
_CLOUDFLARE_RESTART_WAIT_SECONDS = 10.0
_CLOUDFLARE_RESTART_POLL_SECONDS = 0.25
BRIDGE_LABEL = "com.ntlx.serverfs.bridge"
LEGACY_BRIDGE_LABEL = "com.ntlx.serverfs.agent-bridge"
_SERVICE_LABELS = {
    TUNNEL_SERVICE: "com.ntlx.serverfs.tunnel",
    BRIDGE_SERVICE: BRIDGE_LABEL,
    HTTP_ORIGIN_SERVICE: "com.ntlx.serverfs.http-origin",
    CLOUDFLARE_SERVICE: "com.ntlx.serverfs.cloudflare",
}
MANIFEST_SCHEMA_VERSION = 1
HANDOVER_LABEL = "com.ntlx.serverfs.v0132-handover"
HANDOVER_SCHEMA_VERSION = 1

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TUNNEL_ENV_KEYS = frozenset({"CONTROL_PLANE_API_KEY", "SERVERFS_DATA_HOME"})
_HTTP_ORIGIN_ENV_KEYS = frozenset(
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
_HTTP_ORIGIN_ENV_SNAPSHOT = "http-origin-environment.json"
_PROXY_KEYS = (
    "SERVERFS_PROXY_HOST",
    "SERVERFS_PROXY_PORT",
    "SERVERFS_PROXY_USERNAME",
    "SERVERFS_PROXY_PASSWORD",
)


class DarwinServiceError(Exception):
    """Operator-facing macOS service lifecycle failure."""


def service_home() -> Path:
    # The launchd entry point must locate its manifest before any private environment is loaded.
    # Keep this control-plane directory at the canonical per-user macOS location even when the
    # launched ServerFS child later uses SERVERFS_DATA_HOME for its own application state.
    return Path.home() / "Library" / "Application Support" / "ServerFS" / "service-manager"


def installed_app_path() -> Path:
    return service_home() / APP_NAME


def manifest_path() -> Path:
    return service_home() / "services.json"


def tunnel_environment_path() -> Path:
    return service_home() / "tunnel-environment.json"


def tunnel_proxy_env_path() -> Path:
    return service_home() / "tunnel-proxy.env"


def service_log_dir() -> Path:
    return service_home() / "logs"


def legacy_bridge_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_BRIDGE_LABEL}.plist"


def legacy_bridge_backup_path() -> Path:
    return service_home() / "legacy-agent-bridge.plist"


def _source_root() -> Path:
    module_path = Path(__file__).resolve()
    candidates = (
        module_path.parents[2] / "native" / "macos" / "service_manager",
        module_path.parent / "_darwin_service_manager",
    )
    for root in candidates:
        if (root / "build.sh").is_file():
            return root
    raise DarwinServiceError(
        "macOS Service Manager build assets are unavailable in this ServerFS installation"
    )


def _ensure_private_dir(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
        os.chmod(item, 0o700)
    if not path.is_dir():
        raise DarwinServiceError("private service path is not a directory")
    os.chmod(path, 0o700)


def _atomic_private_write(path: Path, payload: bytes) -> None:
    _ensure_private_dir(path.parent)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise DarwinServiceError("service env file does not exist")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DarwinServiceError("service env file cannot be read") from exc
    values: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        key, sep, raw = candidate.partition("=")
        key = key.strip()
        if not sep or not _ENV_NAME.fullmatch(key):
            raise DarwinServiceError(f"invalid env assignment at line {line_number}")
        if key in values:
            raise DarwinServiceError(f"duplicate {key} at line {line_number}")
        try:
            values[key] = parse_env_value(raw.strip(), key, line_number)
        except ValueError as exc:
            raise DarwinServiceError(str(exc)) from exc
    return values


def _service_environment(values: dict[str, str]) -> dict[str, str]:
    """Return only the private values the managed Tunnel process actually consumes."""
    return {
        key: value
        for key, value in values.items()
        if key in _TUNNEL_ENV_KEYS or key.startswith("SERVERFS_FILE_INGRESS_")
    }


def _dotenv_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_private_runtime_files(values: dict[str, str]) -> None:
    environment = _service_environment(values)
    _atomic_private_write(
        tunnel_environment_path(),
        json.dumps(environment, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
    proxy_lines = [f"{key}={_dotenv_quote(values[key])}" for key in _PROXY_KEYS if key in values]
    payload = ("\n".join(proxy_lines) + ("\n" if proxy_lines else "")).encode("utf-8")
    _atomic_private_write(tunnel_proxy_env_path(), payload)


def _tunnel_id(values: dict[str, str]) -> str:
    value = values.get("CONTROL_PLANE_TUNNEL_ID", "").strip()
    if not value:
        raise DarwinServiceError(
            "CONTROL_PLANE_TUNNEL_ID is required for persistent tunnel service"
        )
    return value


def _bridge_executable(values: dict[str, str]) -> Path:
    override = values.get("SERVERFS_BRIDGE_PYTHON", "").strip()
    python = Path(override) if override else Path(sys.executable)
    executable = python.parent / "serverfs-agent-bridge"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise DarwinServiceError("serverfs-agent-bridge executable is unavailable")
    return executable.resolve()


def _tunnel_arguments(config_path: Path, values: dict[str, str]) -> list[str]:
    args = [
        "-m",
        "serverfs_mcp.cli",
        "tunnel",
        "--config",
        str(config_path.resolve()),
        "--env-file",
        str(tunnel_proxy_env_path()),
        "--tunnel-id",
        _tunnel_id(values),
    ]
    key_value = values.get("CONTROL_PLANE_API_KEY", "").strip()
    key_file = values.get("CONTROL_PLANE_API_KEY_FILE", "").strip()
    if key_value and key_file:
        raise DarwinServiceError(
            "configure exactly one control-plane credential source: CONTROL_PLANE_API_KEY or "
            "CONTROL_PLANE_API_KEY_FILE"
        )
    if not key_value and not key_file:
        raise DarwinServiceError(
            "CONTROL_PLANE_API_KEY or CONTROL_PLANE_API_KEY_FILE is required for persistent tunnel"
        )
    if key_file:
        candidate = Path(key_file).expanduser()
        if not candidate.is_absolute():
            raise DarwinServiceError("CONTROL_PLANE_API_KEY_FILE must be an absolute path")
        args.extend(["--api-key-file", str(candidate.resolve())])
    return args


def _write_manifest(config_path: Path, values: dict[str, str], *, bridge_enabled: bool) -> None:
    logs = service_log_dir().resolve()
    _ensure_private_dir(logs)
    services: dict[str, dict[str, object]] = {
        TUNNEL_SERVICE: {
            "executable": str(Path(sys.executable).absolute()),
            "arguments": _tunnel_arguments(config_path, values),
            "environment_file": str(tunnel_environment_path().resolve()),
            "working_directory": str(config_path.resolve().parent),
            "stdout_log": str(logs / "tunnel.stdout.log"),
            "stderr_log": str(logs / "tunnel.stderr.log"),
        }
    }
    if bridge_enabled:
        from .darwin_bridge_policy import bridge_config_path

        services[BRIDGE_SERVICE] = {
            "executable": str(_bridge_executable(values)),
            "arguments": ["--config", str(bridge_config_path().resolve())],
            "environment_file": None,
            "working_directory": str(config_path.resolve().parent),
            "stdout_log": str(logs / "bridge.stdout.log"),
            "stderr_log": str(logs / "bridge.stderr.log"),
        }
    # Legacy installation/update owns only its two entries. Preserve independently
    # installed opt-in services so updating the old pair cannot orphan their plists.
    existing = _load_manifest_document(optional=True)
    for service in _CLOUDFLARE_SERVICES:
        spec = existing.get("services", {}).get(service)
        if isinstance(spec, dict):
            services[service] = spec
    document = {"schema_version": MANIFEST_SCHEMA_VERSION, "services": services}
    _atomic_private_write(
        manifest_path(), json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _build_candidate_app() -> Path:
    root = _source_root()
    build_root = service_home() / f".build-{os.getpid()}"
    if build_root.exists():
        shutil.rmtree(build_root)
    _ensure_private_dir(build_root)
    try:
        completed = subprocess.run(
            ["bash", str(root / "build.sh"), str(build_root)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(build_root, ignore_errors=True)
        raise DarwinServiceError(
            f"macOS Service Manager build could not run ({type(exc).__name__})"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"exit {completed.returncode}"
        shutil.rmtree(build_root, ignore_errors=True)
        raise DarwinServiceError(f"macOS Service Manager build failed: {reason}")
    candidate = build_root / APP_NAME
    if not candidate.is_dir():
        raise DarwinServiceError("macOS Service Manager build produced no app bundle")
    _verify_app(candidate)
    return candidate


def _verify_app(app: Path) -> None:
    info = app / "Contents" / "Info.plist"
    checks = (
        (["codesign", "--verify", "--deep", "--strict", str(app)], "code signature"),
        (["plutil", "-extract", "CFBundleIdentifier", "raw", str(info)], "bundle identifier"),
        (
            ["plutil", "-extract", "CFBundleShortVersionString", "raw", str(info)],
            "bundle version",
        ),
    )
    values: list[str] = []
    for command, name in checks:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DarwinServiceError(
                f"macOS Service Manager {name} verification could not run"
            ) from exc
        if completed.returncode != 0:
            raise DarwinServiceError(f"macOS Service Manager {name} verification failed")
        values.append(completed.stdout.strip())
    if values[1] != APP_BUNDLE_ID or values[2] != APP_VERSION:
        raise DarwinServiceError("macOS Service Manager bundle identity/version mismatch")

    expected_agents = {
        "com.ntlx.serverfs.tunnel.plist": ("com.ntlx.serverfs.tunnel", TUNNEL_SERVICE),
        "com.ntlx.serverfs.bridge.plist": (BRIDGE_LABEL, BRIDGE_SERVICE),
    }
    agents_dir = app / "Contents" / "Library" / "LaunchAgents"
    for filename, (label, service_id) in expected_agents.items():
        path = agents_dir / filename
        try:
            document = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, TypeError) as exc:
            raise DarwinServiceError(
                "macOS Service Manager embedded LaunchAgent is invalid"
            ) from exc
        if (
            not isinstance(document, dict)
            or document.get("Label") != label
            or document.get("BundleProgram") != "Contents/Resources/serverfs-service-launcher"
            or document.get("ProgramArguments") != ["serverfs-service-launcher", service_id]
        ):
            raise DarwinServiceError("macOS Service Manager embedded LaunchAgent contract mismatch")


def _install_candidate_app(candidate: Path) -> Path:
    destination = installed_app_path()
    _ensure_private_dir(destination.parent)
    staging = destination.parent / f".{APP_NAME}.tmp-{os.getpid()}"
    backup = destination.parent / f".{APP_NAME}.old-{os.getpid()}"
    for transient in (staging, backup):
        if transient.exists():
            shutil.rmtree(transient)
    shutil.copytree(candidate, staging, symlinks=True)
    _verify_app(staging)
    had_destination = destination.exists()
    if had_destination:
        os.replace(destination, backup)
    try:
        os.replace(staging, destination)
        _verify_app(destination)
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        if had_destination and backup.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)
    return destination


def manager_executable() -> Path:
    return installed_app_path() / "Contents" / "MacOS" / "serverfs-service-manager"


def _run_manager(*args: str) -> dict:
    executable = manager_executable()
    if not executable.is_file():
        raise DarwinServiceError("macOS Service Manager is not installed")
    try:
        completed = subprocess.run(
            [str(executable), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DarwinServiceError(
            f"macOS Service Manager could not run ({type(exc).__name__})"
        ) from exc
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise DarwinServiceError("macOS Service Manager returned invalid output") from exc
    if not isinstance(payload, dict):
        raise DarwinServiceError("macOS Service Manager returned invalid output")
    if completed.returncode != 0 or payload.get("ok") is not True:
        details: list[str] = []
        errors = payload.get("errors")
        if isinstance(errors, dict):
            details.extend(
                f"{key}: {value}"
                for key, value in sorted(errors.items())
                if isinstance(key, str) and isinstance(value, str) and value.strip()
            )
        services = payload.get("services")
        if isinstance(services, dict):
            state_text = ", ".join(
                f"{key}={value}"
                for key, value in sorted(services.items())
                if isinstance(key, str) and isinstance(value, str)
            )
            if state_text:
                details.append(f"states: {state_text}")
        if not details and completed.stderr.strip():
            details.append(completed.stderr.strip().splitlines()[0])
        suffix = f" ({'; '.join(details)})" if details else ""
        raise DarwinServiceError(f"macOS Service Manager refused the lifecycle operation{suffix}")
    return payload


def native_status(service: str = "all") -> dict[str, str]:
    payload = _run_manager("status", service)
    services = payload.get("services")
    if not isinstance(services, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in services.items()
    ):
        raise DarwinServiceError("macOS Service Manager returned an invalid status document")
    return dict(services)


def _register(service: str = "all") -> dict[str, str]:
    payload = _run_manager("register", service)
    services = payload.get("services")
    if not isinstance(services, dict):
        raise DarwinServiceError("macOS Service Manager returned an invalid registration document")
    return {str(key): str(value) for key, value in services.items()}


def _unregister(service: str = "all") -> dict[str, str]:
    payload = _run_manager("unregister", service)
    services = payload.get("services")
    if not isinstance(services, dict):
        raise DarwinServiceError("macOS Service Manager returned an invalid unregister document")
    return {str(key): str(value) for key, value in services.items()}


def _wait_unregistered(service: str, *, timeout_seconds: float = 10.0) -> None:
    """Wait until ServiceManagement has finished an unregister operation."""
    deadline = time.monotonic() + timeout_seconds
    last_state = "unknown"
    while time.monotonic() < deadline:
        states = native_status(service)
        last_state = states.get(service, "unknown")
        if last_state in {"not-registered", "not-found"}:
            return
        time.sleep(0.1)
    raise DarwinServiceError(
        f"{service} did not finish unregistering before re-registration (state={last_state})"
    )


def _unregister_and_wait(service: str) -> dict[str, str]:
    pid = _managed_job_pid(service)
    states = _unregister(service)
    _wait_unregistered(service)
    if pid is not None:
        _wait_process_exit(pid, description=f"{service} managed process")
    return states


def legacy_status() -> str | None:
    path = legacy_bridge_plist_path()
    if not path.exists() or not manager_executable().is_file():
        return None
    payload = _run_manager("legacy-status", str(path))
    value = payload.get("status")
    return value if isinstance(value, str) else None


def _bootout_legacy_bridge() -> None:
    """Stop the v0.13.0/v0.13.1 job without deleting its rollback plist."""
    try:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{LEGACY_BRIDGE_LABEL}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DarwinServiceError(
            f"legacy Agent Bridge could not be stopped ({type(exc).__name__})"
        ) from exc


def _stage_legacy_bridge_plist() -> None:
    path = legacy_bridge_plist_path()
    backup = legacy_bridge_backup_path()
    if not path.is_file():
        raise DarwinServiceError("legacy Agent Bridge plist disappeared during hand-over")
    _ensure_private_dir(backup.parent)
    if backup.exists():
        raise DarwinServiceError("stale legacy Agent Bridge rollback backup already exists")
    try:
        os.replace(path, backup)
        os.chmod(backup, 0o600)
    except OSError as exc:
        raise DarwinServiceError(
            "legacy Agent Bridge plist could not be staged for hand-over"
        ) from exc


def _restore_legacy_bridge_plist() -> None:
    path = legacy_bridge_plist_path()
    backup = legacy_bridge_backup_path()
    if path.is_file() or not backup.is_file():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup, path)
    except OSError as exc:
        raise DarwinServiceError(
            "legacy Agent Bridge rollback plist could not be restored"
        ) from exc


def _rollback_legacy_bridge() -> None:
    _restore_legacy_bridge_plist()
    path = legacy_bridge_plist_path()
    if not path.is_file():
        raise DarwinServiceError("legacy Agent Bridge rollback plist is unavailable")
    try:
        completed = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DarwinServiceError(
            f"legacy Agent Bridge rollback could not bootstrap ({type(exc).__name__})"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise DarwinServiceError("legacy Agent Bridge rollback bootstrap failed" + suffix)


def _migrate_legacy_bridge(*, bridge_enabled: bool) -> None:
    path = legacy_bridge_plist_path()
    if not path.exists():
        return
    # v0.13.2 uses a different bundled Bridge label, but the legacy and bundled jobs bind the
    # same private AF_UNIX endpoint. Boot out and fully drain the legacy process before registering
    # the replacement. Stage the legacy plist outside Background Task Management's discovery
    # location while preserving a byte-for-byte rollback copy until the bundled Bridge has passed
    # its health probe.
    _unregister_and_wait(BRIDGE_SERVICE)
    legacy_pid = _launchd_job_pid(LEGACY_BRIDGE_LABEL)
    _bootout_legacy_bridge()
    if legacy_pid is not None:
        _wait_process_exit(legacy_pid, description="legacy Agent Bridge")
    if not bridge_enabled:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _stage_legacy_bridge_plist()
    try:
        states = _register(BRIDGE_SERVICE)
    except DarwinServiceError:
        _rollback_legacy_bridge()
        raise
    if states.get(BRIDGE_SERVICE) == "requires-approval":
        try:
            _unregister_and_wait(BRIDGE_SERVICE)
        finally:
            _rollback_legacy_bridge()
        raise DarwinServiceError(
            "Agent Bridge registration requires approval in System Settings > General > Login Items"
        )
    if states.get(BRIDGE_SERVICE) != "enabled":
        try:
            _unregister_and_wait(BRIDGE_SERVICE)
        finally:
            _rollback_legacy_bridge()
        raise DarwinServiceError("new Agent Bridge did not become enabled")


def _wait_managed_job(service: str, *, timeout_seconds: float = 20.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pid = _managed_job_pid(service)
        if pid is not None:
            return pid
        time.sleep(0.25)
    raise DarwinServiceError(f"{service} is enabled but no managed process became active")


def _wait_bridge_health(*, timeout_seconds: float = 20.0) -> None:
    from .agent_client import AgentBridgeClient
    from .native_endpoint import derive_endpoint

    async def ping() -> None:
        client = AgentBridgeClient(Path(derive_endpoint()), timeout_seconds=10.0)
        await client.call("bridge.ping", {})

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            asyncio.run(ping())
            return
        except Exception as exc:  # noqa: BLE001 - bounded live lifecycle probe
            last_error = exc
            time.sleep(0.25)
    failure = type(last_error).__name__ if last_error else "timeout"
    raise DarwinServiceError(f"new Agent Bridge failed its health probe ({failure})")


def _finalize_legacy_bridge_migration() -> None:
    paths = (legacy_bridge_plist_path(), legacy_bridge_backup_path())
    try:
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise DarwinServiceError(
            "new Agent Bridge is healthy but legacy rollback artifact removal failed"
        ) from exc


def _launchd_job_pid(label: str) -> int | None:
    try:
        completed = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            value = stripped.removeprefix("pid = ").strip()
            return int(value) if value.isdecimal() else None
    return None


def _managed_job_pid(service: str) -> int | None:
    return _launchd_job_pid(_SERVICE_LABELS[service])


def _wait_process_exit(
    pid: int, *, description: str = "process", timeout_seconds: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        time.sleep(0.1)
    raise DarwinServiceError(f"{description} pid {pid} did not exit")


def find_unmanaged_tunnel_pids() -> list[int]:
    """Return an unmanaged ServerFS tunnel process tree without killing anything.

    v0.13.1 could leave tunnel-client/supervisor descendants orphaned when its Python
    ``serverfs tunnel`` parent received SIGTERM. Detect both a live top-level launcher and
    such an orphaned tunnel-client root, then include their descendants. Descendants of the
    v0.13.2 launchd-managed tunnel are explicitly excluded.
    """
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []

    records: dict[int, tuple[int, str]] = {}
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3 or not fields[0].isdecimal() or not fields[1].isdecimal():
            continue
        records[int(fields[0])] = (int(fields[1]), fields[2])

    def process_tree(roots: set[int]) -> set[int]:
        tree = set(roots)
        changed = True
        while changed:
            changed = False
            for pid, (ppid, _command) in records.items():
                if ppid in tree and pid not in tree:
                    tree.add(pid)
                    changed = True
        return tree

    managed_pid = _managed_job_pid(TUNNEL_SERVICE)
    managed_tree = process_tree({managed_pid}) if managed_pid is not None else set()
    roots: set[int] = set()
    for pid, (_ppid, command) in records.items():
        top_level = "serverfs_mcp.cli tunnel" in command or "serverfs tunnel" in command
        orphanable_client = "tunnel-client" in command and "serverfs_mcp.supervisor" in command
        if (top_level or orphanable_client) and pid != os.getpid() and pid not in managed_tree:
            roots.add(pid)

    return sorted(process_tree(roots) - managed_tree - {os.getpid()})


def find_unmanaged_tunnel_roots() -> list[int]:
    """Return stable root PIDs for unmanaged Tunnel trees.

    Descendants can legitimately appear/disappear while an Agent task is active. Migration freezes
    only roots, then re-discovers the full tree immediately before cut-over.
    """
    unmanaged = set(find_unmanaged_tunnel_pids())
    if not unmanaged:
        return []
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    parents: dict[int, int] = {}
    for line in completed.stdout.splitlines():
        fields = line.strip().split()
        if len(fields) == 2 and fields[0].isdecimal() and fields[1].isdecimal():
            parents[int(fields[0])] = int(fields[1])
    return sorted(pid for pid in unmanaged if parents.get(pid) not in unmanaged)


def handover_plist_path() -> Path:
    return service_home() / "handover.plist"


def handover_result_path() -> Path:
    return service_home() / "handover-result.json"


def _handover_label_target() -> str:
    return f"gui/{os.getuid()}/{HANDOVER_LABEL}"


def _bootout_handover_job() -> None:
    try:
        subprocess.run(
            ["launchctl", "bootout", _handover_label_target()],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DarwinServiceError("temporary macOS hand-over job could not be unloaded") from exc
    probe = subprocess.run(
        ["launchctl", "print", _handover_label_target()],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if probe.returncode == 0:
        raise DarwinServiceError("temporary macOS hand-over job remains loaded")


def _remove_handover_artifacts() -> None:
    _bootout_handover_job()
    handover_plist_path().unlink(missing_ok=True)
    if handover_plist_path().exists():
        raise DarwinServiceError("temporary macOS hand-over plist remains")


def write_handover_result(*, ok: bool, detail: str, complete: bool = True) -> None:
    payload = {
        "schema_version": HANDOVER_SCHEMA_VERSION,
        "ok": ok,
        "complete": complete,
        "detail": detail,
        "timestamp": time.time(),
    }
    _atomic_private_write(
        handover_result_path(),
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def schedule_handover(
    operation: str,
    config_path: Path | None = None,
    env_file: Path | None = None,
    root_pids: list[int] | None = None,
) -> None:
    """Run a lifecycle-sensitive operation in an independent one-shot launchd job.

    An install may provide frozen unmanaged Tunnel roots. Restart is scheduled only when the
    managed Agent Bridge is present, so Bridge unregister cannot terminate its caller.
    """
    if operation not in {"install", "restart", "bridge-restart"}:
        raise DarwinServiceError("unsupported macOS hand-over operation")
    _ensure_private_dir(service_home())
    _ensure_private_dir(service_log_dir())
    _bootout_handover_job()
    try:
        handover_result_path().unlink()
    except FileNotFoundError:
        pass
    command = (
        ["service", "restart"]
        if operation == "restart"
        else ["agent-bridge", "restart"]
        if operation == "bridge-restart"
        else ["service", "install"]
    )
    arguments = [str(Path(sys.executable).absolute()), "-m", "serverfs_mcp.cli", *command]
    if operation == "install":
        if config_path is None or env_file is None:
            raise DarwinServiceError("install hand-over requires config and env paths")
        arguments.extend(
            ["--config", str(config_path.resolve()), "--env-file", str(env_file.resolve())]
        )
    arguments.append("--_handover-child")
    document = {
        "Label": HANDOVER_LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(
            config_path.resolve().parent if config_path else service_home().resolve()
        ),
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Background",
        "StandardOutPath": str(service_log_dir() / "handover.stdout.log"),
        "StandardErrorPath": str(service_log_dir() / "handover.stderr.log"),
    }
    if operation == "install":
        document["ProgramArguments"].extend(
            ["--_handover-roots", ",".join(str(pid) for pid in (root_pids or []))]
        )
    _atomic_private_write(handover_plist_path(), plistlib.dumps(document, fmt=plistlib.FMT_XML))
    domain = f"gui/{os.getuid()}"
    try:
        bootstrapped = subprocess.run(
            ["launchctl", "bootstrap", domain, str(handover_plist_path())],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if bootstrapped.returncode != 0:
            _remove_handover_artifacts()
            write_handover_result(
                ok=False, detail="temporary macOS hand-over job could not be bootstrapped"
            )
            raise DarwinServiceError("temporary macOS hand-over job could not be bootstrapped")
        started = subprocess.run(
            ["launchctl", "kickstart", _handover_label_target()],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if started.returncode != 0:
            _remove_handover_artifacts()
            write_handover_result(
                ok=False, detail="temporary macOS hand-over job could not be started"
            )
            raise DarwinServiceError("temporary macOS hand-over job could not be started")
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            _remove_handover_artifacts()
        except DarwinServiceError as cleanup_exc:
            write_handover_result(
                ok=False,
                detail=(
                    f"handover scheduling failed ({type(exc).__name__}); "
                    f"cleanup failed: {cleanup_exc}"
                ),
            )
            raise
        write_handover_result(ok=False, detail=f"handover scheduling failed ({type(exc).__name__})")
        raise DarwinServiceError(
            f"temporary macOS hand-over job failed ({type(exc).__name__})"
        ) from exc


def schedule_handover_install(config_path: Path, env_file: Path, root_pids: list[int]) -> None:
    """Compatibility wrapper for the install hand-over path."""
    schedule_handover("install", config_path, env_file, root_pids)


def cleanup_handover_job() -> None:
    """Unload the one-shot job, verify cleanup, and publish final operation evidence."""
    pending_ok = False
    pending_detail = "operation child produced no pending result"
    try:
        pending = json.loads(handover_result_path().read_text(encoding="utf-8"))
        if pending.get("complete") is False and isinstance(pending.get("ok"), bool):
            pending_ok = pending["ok"]
            pending_detail = str(pending.get("detail", "operation completed"))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        _remove_handover_artifacts()
    except (OSError, subprocess.SubprocessError, DarwinServiceError) as exc:
        detail = f"{pending_detail}; cleanup failed: {exc}"
        write_handover_result(ok=False, detail=detail)
        if isinstance(exc, DarwinServiceError):
            raise
        raise DarwinServiceError("temporary macOS hand-over cleanup could not be verified") from exc
    write_handover_result(ok=pending_ok, detail=pending_detail)


def schedule_handover_cleanup() -> None:
    """Start an independent bounded process to unload the completed launchd job."""
    try:
        subprocess.Popen(
            [
                str(Path(sys.executable).absolute()),
                "-m",
                "serverfs_mcp.cli",
                "service",
                "_handover-cleanup",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        try:
            pending = json.loads(handover_result_path().read_text(encoding="utf-8"))
            previous = str(pending.get("detail", "operation result unavailable"))
        except (OSError, ValueError, AttributeError):
            previous = "operation result unavailable"
        write_handover_result(ok=False, detail=f"{previous}; cleanup helper could not start")
        raise DarwinServiceError("temporary macOS hand-over cleanup could not be started") from exc


def _terminate_frozen_unmanaged_tunnel(root_pids: list[int]) -> None:
    expected_roots = sorted(set(root_pids))
    current_roots = find_unmanaged_tunnel_roots()
    if current_roots != expected_roots:
        raise DarwinServiceError(
            "unmanaged Tunnel roots changed before hand-over; refusing to terminate processes"
        )
    current_tree = find_unmanaged_tunnel_pids()
    if not current_tree:
        return
    for pid in reversed(current_tree):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise DarwinServiceError(f"could not stop unmanaged Tunnel pid {pid}") from exc
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        remaining = find_unmanaged_tunnel_pids()
        if not remaining:
            return
        time.sleep(0.2)
    remaining = find_unmanaged_tunnel_pids()
    for pid in reversed(remaining):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise DarwinServiceError(f"could not force-stop unmanaged Tunnel pid {pid}") from exc
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not find_unmanaged_tunnel_pids():
            return
        time.sleep(0.1)
    raise DarwinServiceError("unmanaged Tunnel processes survived the hand-over stop window")


def _prepare_install(config_path: Path, env_file: Path, *, bridge_enabled: bool) -> None:
    values = _read_env_file(env_file.resolve())
    _write_private_runtime_files(values)
    _write_manifest(config_path, values, bridge_enabled=bridge_enabled)
    candidate = _build_candidate_app()
    try:
        _install_candidate_app(candidate)
    finally:
        if candidate.parent.exists():
            shutil.rmtree(candidate.parent)


def _activate_prepared_install(*, bridge_enabled: bool) -> dict[str, str]:
    # Bring the control-plane path under launchd before touching the currently running Bridge.
    tunnel_states = _register(TUNNEL_SERVICE)
    tunnel_state = tunnel_states.get(TUNNEL_SERVICE)
    if tunnel_state == "requires-approval":
        raise DarwinServiceError(
            "tunnel requires approval in System Settings > General > Login Items"
        )
    if tunnel_state != "enabled":
        raise DarwinServiceError("tunnel did not become enabled")
    _wait_managed_job(TUNNEL_SERVICE)

    legacy_bridge = legacy_bridge_plist_path().is_file()
    if legacy_bridge:
        _migrate_legacy_bridge(bridge_enabled=bridge_enabled)
    elif bridge_enabled:
        bridge_states = _register(BRIDGE_SERVICE)
        bridge_state = bridge_states.get(BRIDGE_SERVICE)
        if bridge_state == "requires-approval":
            raise DarwinServiceError(
                "Agent Bridge requires approval in System Settings > General > Login Items"
            )
        if bridge_state != "enabled":
            raise DarwinServiceError("Agent Bridge did not become enabled")
    else:
        _unregister_and_wait(BRIDGE_SERVICE)

    if bridge_enabled:
        try:
            _wait_managed_job(BRIDGE_SERVICE)
            _wait_bridge_health()
        except DarwinServiceError:
            _unregister_and_wait(BRIDGE_SERVICE)
            if legacy_bridge:
                _rollback_legacy_bridge()
            raise
        if legacy_bridge:
            _finalize_legacy_bridge_migration()
    return native_status()


def install(
    config_path: Path,
    env_file: Path,
    *,
    bridge_enabled: bool,
    expected_unmanaged_roots: list[int] | None = None,
) -> dict[str, str]:
    # The legacy installer replaces the manifest and app-owned service bundle.
    # It must not overwrite separately owned v0.25.0 Core/Connector jobs.
    _refuse_legacy_group_lifecycle(_load_manifest_document(optional=True))
    from .darwin_platform import ensure_supported_darwin

    ensure_supported_darwin()
    current_roots = find_unmanaged_tunnel_roots()
    if expected_unmanaged_roots is None:
        if current_roots:
            raise DarwinServiceError(
                "an unmanaged v0.13.1 tunnel is still running; use the controlled hand-over"
            )
    elif current_roots != sorted(set(expected_unmanaged_roots)):
        raise DarwinServiceError("unmanaged Tunnel roots changed before hand-over preparation")

    # Build/install the native bundle while the old Tunnel is still alive. Only cut the old
    # control-plane path after every expensive/preflight step has succeeded.
    _prepare_install(config_path, env_file, bridge_enabled=bridge_enabled)
    if expected_unmanaged_roots:
        _terminate_frozen_unmanaged_tunnel(expected_unmanaged_roots)
    return _activate_prepared_install(bridge_enabled=bridge_enabled)


def register_service(service: str) -> dict[str, str]:
    if service not in ALL_SERVICES:
        raise DarwinServiceError("unknown persistent service")
    return _register_and_wait(service)


def unregister_service(service: str) -> dict[str, str]:
    if service not in ALL_SERVICES:
        raise DarwinServiceError("unknown persistent service")
    return _unregister_and_wait(service)


def _register_and_wait(service: str) -> dict[str, str]:
    if service == TUNNEL_SERVICE:
        manifest = _load_manifest_document()
        spec = manifest["services"].get(TUNNEL_SERVICE)
        if (
            isinstance(spec, dict)
            and spec.get("connector_owner") == "independent-openai-http"
            and find_unmanaged_tunnel_pids()
        ):
            raise DarwinServiceError("unmanaged OpenAI Tunnel process is still running")
    states = _register(service)
    state = states.get(service)
    if state == "requires-approval":
        label = "Agent Bridge" if service == BRIDGE_SERVICE else service
        raise DarwinServiceError(
            f"{label} requires approval in System Settings > General > Login Items"
        )
    if state != "enabled":
        raise DarwinServiceError(f"{service} did not become enabled")
    _wait_managed_job(service)
    if service == BRIDGE_SERVICE:
        _wait_bridge_health()
    return states


def _secure_input_file(path: Path, name: str) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise DarwinServiceError(f"{name} must be an absolute path")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise DarwinServiceError(f"{name} path must not contain symlinks")
    try:
        stat = candidate.stat(follow_symlinks=False)
    except OSError as exc:
        raise DarwinServiceError(f"{name} is unavailable") from exc
    if not candidate.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o777 != 0o600:
        raise DarwinServiceError(f"{name} must be a current-user regular file with mode 0600")
    return candidate


def _reject_symlink_components(path: Path, name: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise DarwinServiceError(f"{name} path must not contain symlinks")


def _validate_private_service_home() -> None:
    home = service_home()
    _reject_symlink_components(home, "private service directory")
    if not home.exists():
        return
    try:
        stat = home.stat(follow_symlinks=False)
    except OSError as exc:
        raise DarwinServiceError("private service directory is unavailable") from exc
    if not home.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise DarwinServiceError("private service directory must be current-user mode 0700")


def _validate_private_location(path: Path, forbidden_roots: tuple[Path, ...], name: str) -> Path:
    resolved = path.resolve(strict=True)
    if any(resolved == root or root in resolved.parents for root in forbidden_roots):
        raise DarwinServiceError(f"{name} must be outside the checkout and every workdir")
    return resolved


def _validated_http_origin_environment(env: dict[str, str], hostname: str) -> dict[str, str]:
    """Validate and return only the HTTP-origin settings safe to snapshot."""
    unexpected_env = set(env) - _HTTP_ORIGIN_ENV_KEYS
    if unexpected_env:
        raise DarwinServiceError("HTTP origin env file contains unsupported settings")
    if env.get("SERVERFS_HTTP_ALLOWED_HOSTS") != hostname:
        raise DarwinServiceError("SERVERFS_HTTP_ALLOWED_HOSTS must exactly match public hostname")
    from .config import settings_from_env

    try:
        auth_settings = settings_from_env(env)
    except ValueError as exc:
        raise DarwinServiceError("HTTP origin authentication configuration is invalid") from exc
    api_key = bool(auth_settings.http_api_key)
    oauth = auth_settings.oauth_enabled
    if api_key == oauth:
        raise DarwinServiceError("configure exactly one HTTP API Key or OAuth authentication")
    if oauth and not env.get("SERVERFS_OAUTH_INTROSPECTION_SECRET", "").strip():
        raise DarwinServiceError("OAuth requires SERVERFS_OAUTH_INTROSPECTION_SECRET")
    if "SERVERFS_HTTP_API_KEY" in env and oauth:
        raise DarwinServiceError("HTTP API Key and OAuth cannot be combined")
    return dict(env)


def _loopback_port_available(port: int) -> bool:
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _http_origin_spec(
    config_path: Path,
    env_file: Path,
    port: int,
    hostname: str,
    *,
    shared_core: bool = False,
) -> tuple[dict[str, object], tuple[Path, ...]]:
    """Validate one authenticated HTTP service, without connector credentials."""
    from .native_config import load_native_config

    label = "Core" if shared_core else "Cloudflare"
    valid_hostname = len(hostname) <= 253 and all(
        1 <= len(part) <= 63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", part)
        for part in hostname.split(".")
    )
    if not 1 <= port <= 65535 or not valid_hostname:
        raise DarwinServiceError(f"invalid {label} origin port or hostname")
    if not _loopback_port_available(port):
        raise DarwinServiceError("HTTP origin loopback port is unavailable")

    config_path = config_path.expanduser()
    if not config_path.is_absolute():
        raise DarwinServiceError(f"{label} serverfs.toml path must be absolute")
    _reject_symlink_components(config_path, f"{label} serverfs.toml")
    try:
        config_path = config_path.resolve(strict=True)
    except OSError as exc:
        raise DarwinServiceError(f"{label} native configuration is unavailable") from exc
    if not config_path.is_file() or config_path.suffix.lower() != ".toml":
        raise DarwinServiceError(f"{label} service requires a native serverfs.toml file")
    try:
        workdirs, _settings = load_native_config(config_path)
    except Exception as exc:
        if "at least one [[workdirs]] entry is required" in str(exc):
            raise DarwinServiceError(f"{label} service requires at least one workdir") from exc
        raise DarwinServiceError(f"{label} native configuration is invalid") from exc
    if not workdirs:
        raise DarwinServiceError(f"{label} service requires at least one workdir")
    roots_list: list[Path] = []
    for item in workdirs:
        root = Path(item.root).expanduser()
        _reject_symlink_components(root, f"{label} workdir")
        roots_list.append(root.resolve(strict=True))
    roots = tuple(roots_list)

    env_file = _secure_input_file(env_file, "HTTP origin env file")
    _validate_private_location(
        env_file, (Path(__file__).resolve().parents[2], *roots), "HTTP origin env file"
    )
    expected_hosts = f"{hostname},127.0.0.1:{port}" if shared_core else hostname
    _validated_http_origin_environment(_read_env_file(env_file), expected_hosts)
    _validate_private_service_home()
    environment_file = service_home() / _HTTP_ORIGIN_ENV_SNAPSHOT
    logs = service_log_dir()
    _reject_symlink_components(logs, "service log directory")
    _ensure_private_dir(logs)
    return (
        {
            # Preserve the venv interpreter path, including its symlink.
            "executable": str(Path(sys.executable).absolute()),
            "arguments": [
                "-m",
                "serverfs_mcp.cli",
                "serve",
                "--transport",
                "streamable-http",
                "--config",
                str(config_path),
                "--http-port",
                str(port),
            ],
            "environment_file": str(environment_file.resolve()),
            "working_directory": str(config_path.parent),
            "stdout_log": str(logs / "http-origin.stdout.log"),
            "stderr_log": str(logs / "http-origin.stderr.log"),
        },
        roots,
    )


def _core_service_spec(
    config_path: Path, env_file: Path, port: int, hostname: str
) -> dict[str, object]:
    spec, _roots = _http_origin_spec(config_path, env_file, port, hostname, shared_core=True)
    return spec


def _cloudflare_service_specs(
    config_path: Path,
    cloudflared: Path,
    token_file: Path,
    env_file: Path,
    port: int,
    hostname: str,
) -> dict[str, dict[str, object]]:
    origin, roots = _http_origin_spec(config_path, env_file, port, hostname)
    return {
        HTTP_ORIGIN_SERVICE: origin,
        CLOUDFLARE_SERVICE: _cloudflared_spec(
            cloudflared, token_file, roots, Path(str(origin["working_directory"]))
        ),
    }


def _cloudflared_spec(
    cloudflared: Path, token_file: Path, workdir_roots: tuple[Path, ...], cwd: Path
) -> dict[str, object]:
    """Build only the connector definition; never write, register or launch it."""
    token_file = _secure_input_file(token_file, "Cloudflare connector token file")
    _validate_private_location(
        token_file, (Path(__file__).resolve().parents[2], *workdir_roots),
        "Cloudflare token file",
    )
    cloudflared = cloudflared.expanduser()
    if not cloudflared.is_absolute():
        raise DarwinServiceError("cloudflared must be an existing fixed absolute executable")
    _reject_symlink_components(cloudflared, "cloudflared")
    try:
        binary_stat = cloudflared.stat(follow_symlinks=False)
    except OSError as exc:
        raise DarwinServiceError("cloudflared is unavailable") from exc
    if (
        not cloudflared.is_file()
        or binary_stat.st_uid != os.getuid()
        or binary_stat.st_mode & 0o022
    ):
        raise DarwinServiceError("cloudflared must be an existing fixed absolute executable")
    if not os.access(cloudflared, os.X_OK):
        raise DarwinServiceError("cloudflared is not executable")
    logs = service_log_dir()
    _reject_symlink_components(logs, "service log directory")
    _ensure_private_dir(logs)
    return {
        "executable": str(cloudflared),
        "arguments": ["tunnel", "--no-autoupdate", "run", "--token-file", str(token_file)],
        "environment_file": None,
        "working_directory": str(cwd),
        "stdout_log": str(logs / "cloudflare.stdout.log"),
        "stderr_log": str(logs / "cloudflare.stderr.log"),
    }


def install_cloudflare_service(
    config_path: Path, cloudflared: Path, token_file: Path, env_file: Path, port: int, hostname: str
) -> dict[str, str]:
    _validate_private_service_home()
    if not manager_executable().is_file():
        raise DarwinServiceError("installed macOS Service Manager is unavailable")
    _verify_app(installed_app_path())
    _verify_opt_in_agents(installed_app_path())
    current = native_status("http-origin") | native_status("cloudflare")
    if any(
        current.get(service) not in {"not-registered", "not-found"}
        for service in _CLOUDFLARE_SERVICES
    ):
        raise DarwinServiceError(
            "Cloudflare service pair is already registered; refusing replacement"
        )
    _refuse_unmanaged_cloudflared()
    specs = _cloudflare_service_specs(
        config_path, cloudflared, token_file, env_file, port, hostname
    )
    origin_environment = _validated_http_origin_environment(
        _read_env_file(_secure_input_file(env_file, "HTTP origin env file")), hostname
    )
    document = _load_manifest_document(optional=True)
    services = document.get("services", {})
    if any(service in services for service in _CLOUDFLARE_SERVICES):
        raise DarwinServiceError("Cloudflare manifest entries already exist; refusing replacement")
    manifest_existed = manifest_path().exists()
    services.update(specs)
    try:
        _atomic_private_write(
            service_home() / _HTTP_ORIGIN_ENV_SNAPSHOT,
            json.dumps(origin_environment, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        _atomic_private_write(
            manifest_path(),
            json.dumps(
                {"schema_version": MANIFEST_SCHEMA_VERSION, "services": services},
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        )
    except Exception:
        try:
            (service_home() / _HTTP_ORIGIN_ENV_SNAPSHOT).unlink()
        except FileNotFoundError:
            pass
        raise
    attempted: list[str] = []
    try:
        for service in _CLOUDFLARE_SERVICES:
            attempted.append(service)
            result = _register(service)
            state = result.get(service)
            if state != "enabled":
                raise DarwinServiceError(f"{service} registration did not become enabled")
            _wait_managed_job(service)
            if service == HTTP_ORIGIN_SERVICE:
                _wait_http_origin(port)
        return {**native_status("http-origin"), **native_status("cloudflare")}
    except Exception as original_error:
        rollback_complete = True
        for service in reversed(attempted):
            try:
                state = native_status(service).get(service)
                if state not in {"not-registered", "not-found"}:
                    result = _unregister_and_wait(service)
                    state = result.get(service)
                    if state not in {"not-registered", "not-found"}:
                        rollback_complete = False
            except Exception:
                rollback_complete = False
        if not rollback_complete:
            raise DarwinServiceError(
                "Cloudflare installation rollback incomplete; preserved for recovery"
            ) from None
        document["services"] = {
            key: value for key, value in services.items() if key not in _CLOUDFLARE_SERVICES
        }
        if manifest_existed:
            _atomic_private_write(
                manifest_path(),
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
            )
        else:
            try:
                manifest_path().unlink()
            except FileNotFoundError:
                pass
        try:
            (service_home() / _HTTP_ORIGIN_ENV_SNAPSHOT).unlink()
        except FileNotFoundError:
            pass
        raise original_error


def _wait_http_origin(port: int, *, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/mcp"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    raise DarwinServiceError("HTTP origin failed its bounded loopback health probe")


def _verify_opt_in_agents(app: Path) -> None:
    expected = {
        "com.ntlx.serverfs.http-origin.plist": (
            "com.ntlx.serverfs.http-origin",
            HTTP_ORIGIN_SERVICE,
        ),
        "com.ntlx.serverfs.cloudflare.plist": (
            "com.ntlx.serverfs.cloudflare",
            CLOUDFLARE_SERVICE,
        ),
    }
    agents = app / "Contents" / "Library" / "LaunchAgents"
    for filename, (label, service_id) in expected.items():
        try:
            document = plistlib.loads((agents / filename).read_bytes())
        except (OSError, ValueError, TypeError) as exc:
            raise DarwinServiceError(
                "installed Service Manager does not include the opt-in Cloudflare LaunchAgents"
            ) from exc
        if (
            not isinstance(document, dict)
            or document.get("Label") != label
            or document.get("BundleProgram") != "Contents/Resources/serverfs-service-launcher"
            or document.get("ProgramArguments") != ["serverfs-service-launcher", service_id]
        ):
            raise DarwinServiceError("installed opt-in LaunchAgent contract is invalid")


def cloudflare_status() -> dict[str, object]:
    states: dict[str, str] = {}
    processes: dict[str, int | None] = {}
    for service in _CLOUDFLARE_SERVICES:
        state = native_status(service).get(service, "unknown")
        pid = _managed_job_pid(service)
        processes[service] = pid
        if state == "enabled" and pid is None:
            state = "enabled-no-process"
        elif state in {"not-registered", "not-found"} and pid is not None:
            state = "unexpected-process"
        states[service] = state
    return {"services": states, "processes": processes}


def _refuse_unmanaged_cloudflared() -> None:
    managed_pid = _managed_job_pid(CLOUDFLARE_SERVICE)
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DarwinServiceError("cannot verify cloudflared process ownership") from exc
    if completed.returncode != 0:
        raise DarwinServiceError("cannot verify cloudflared process ownership")
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdecimal():
            continue
        pid, command = int(fields[0]), fields[1]
        if (
            pid != managed_pid
            and "cloudflared" in command
            and "tunnel" in command
            and "run" in command
        ):
            raise DarwinServiceError("an unmanaged cloudflared tunnel process is already running")


def cloudflare_start() -> dict[str, str]:
    manifest = _require_cloudflare_manifest()
    _refuse_unmanaged_cloudflared()
    result: dict[str, str] = {}
    for service in _CLOUDFLARE_SERVICES:
        result.update(_register_and_wait(service))
        if service == HTTP_ORIGIN_SERVICE:
            spec = manifest["services"][service]
            port = int(spec["arguments"][spec["arguments"].index("--http-port") + 1])
            _wait_http_origin(port)
    return result


def cloudflare_stop() -> dict[str, str]:
    _require_cloudflare_manifest()
    result: dict[str, str] = {}
    for service in reversed(_CLOUDFLARE_SERVICES):
        result.update(_unregister_and_wait(service))
    return result


def cloudflare_restart() -> dict[str, str]:
    cloudflare_stop()
    deadline = time.monotonic() + _CLOUDFLARE_RESTART_WAIT_SECONDS
    while True:
        try:
            _refuse_unmanaged_cloudflared()
            break
        except DarwinServiceError as exc:
            if str(exc) != "an unmanaged cloudflared tunnel process is already running":
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_CLOUDFLARE_RESTART_POLL_SECONDS, remaining))
    return cloudflare_start()


def cloudflare_uninstall() -> None:
    cloudflare_stop()
    document = _load_manifest_document()
    document["services"] = {
        key: value for key, value in document["services"].items() if key not in _CLOUDFLARE_SERVICES
    }
    _atomic_private_write(
        manifest_path(), json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    )
    try:
        (service_home() / _HTTP_ORIGIN_ENV_SNAPSHOT).unlink()
    except FileNotFoundError:
        pass


def _require_cloudflare_manifest() -> dict:
    manifest = _load_manifest_document()
    if not all(service in manifest["services"] for service in _CLOUDFLARE_SERVICES):
        raise DarwinServiceError("Cloudflare service pair is not installed")
    origin = manifest["services"][HTTP_ORIGIN_SERVICE]
    if isinstance(origin, dict) and origin.get("origin_owner") == "independent-core":
        raise DarwinServiceError(
            "legacy Cloudflare pair lifecycle cannot manage independent Core; "
            "use --component cloudflare"
        )
    return manifest


def _require_shared_core_config(services: dict, config_path: Path) -> None:
    """A managed OpenAI Tunnel and Core must use the same canonical native TOML."""
    tunnel = services.get(TUNNEL_SERVICE)
    if tunnel is None:
        return
    arguments = tunnel.get("arguments") if isinstance(tunnel, dict) else None
    if not isinstance(arguments, list) or arguments.count("--config") != 1:
        raise DarwinServiceError("OpenAI Tunnel configuration cannot be verified for Core")
    index = arguments.index("--config")
    try:
        raw_path = arguments[index + 1]
        if not isinstance(raw_path, str):
            raise ValueError("invalid path")
        tunnel_path = Path(raw_path).expanduser()
        if not tunnel_path.is_absolute():
            raise ValueError("relative path")
        _reject_symlink_components(tunnel_path, "OpenAI Tunnel configuration")
        if tunnel_path.resolve(strict=True) != config_path.expanduser().resolve(strict=True):
            raise ValueError("different configuration")
    except (IndexError, OSError, ValueError) as exc:
        raise DarwinServiceError(
            "independent Core must use the same canonical serverfs.toml as OpenAI Tunnel"
        ) from exc


def stage_core_service(
    config_path: Path, env_file: Path, port: int, hostname: str
) -> dict[str, str]:
    """Stage one private independent Core definition without registering a job.

    Registration is a separate explicit `service start --component core` action.
    Never replace a currently installed Core or the legacy Cloudflare HTTP origin.
    """
    _validate_private_service_home()
    if not manager_executable().is_file():
        raise DarwinServiceError("installed macOS Service Manager is unavailable")
    _verify_app(installed_app_path())
    _verify_opt_in_agents(installed_app_path())
    if native_status(HTTP_ORIGIN_SERVICE).get(HTTP_ORIGIN_SERVICE) not in {
        "not-registered", "not-found"
    }:
        raise DarwinServiceError("Core/HTTP origin is already registered; refusing replacement")
    document = _load_manifest_document(optional=True)
    services = document.get("services", {})
    if HTTP_ORIGIN_SERVICE in services:
        raise DarwinServiceError("Core/HTTP origin manifest already exists; refusing replacement")
    if CLOUDFLARE_SERVICE in services:
        raise DarwinServiceError("Cloudflare connector is installed; Core migration requires review")
    _require_shared_core_config(services, config_path)
    snapshot = service_home() / _HTTP_ORIGIN_ENV_SNAPSHOT
    if snapshot.exists() or snapshot.is_symlink():
        raise DarwinServiceError("Core private environment snapshot already exists")

    spec = _core_service_spec(config_path, env_file, port, hostname)
    private_env = _secure_input_file(env_file, "HTTP origin env file")
    # Re-read and revalidate before publication: no stale caller-auth snapshot.
    expected_hosts = f"{hostname},127.0.0.1:{port}"
    environment = _validated_http_origin_environment(_read_env_file(private_env), expected_hosts)
    spec["origin_owner"] = "independent-core"
    services = {**services, HTTP_ORIGIN_SERVICE: spec}
    _atomic_private_write(
        snapshot, json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()
    )
    try:
        _atomic_private_write(
            manifest_path(),
            json.dumps(
                {"schema_version": MANIFEST_SCHEMA_VERSION, "services": services},
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        )
    except Exception:
        try:
            snapshot.unlink()
        except FileNotFoundError:
            pass
        # The manifest writer is atomic; on write failure the original remains.
        raise
    return {HTTP_ORIGIN_SERVICE: "staged-not-registered"}


def core_status() -> dict[str, object]:
    """Report registration and identity separately; never inspect credential values."""
    status = native_status(HTTP_ORIGIN_SERVICE)
    manifest = _load_manifest_document(optional=True)
    spec = manifest.get("services", {}).get(HTTP_ORIGIN_SERVICE)
    return {
        "service": HTTP_ORIGIN_SERVICE,
        "registration": status.get(HTTP_ORIGIN_SERVICE, "unknown"),
        "manifest_installed": isinstance(spec, dict),
        "independent_core": isinstance(spec, dict)
        and spec.get("origin_owner") == "independent-core",
        "pid": _managed_job_pid(HTTP_ORIGIN_SERVICE),
    }


def unstage_core_service() -> None:
    """Remove only an unregistered v0.25.0 staged Core (not a legacy/live origin)."""
    document = _load_manifest_document()
    services = document["services"]
    spec = services.get(HTTP_ORIGIN_SERVICE)
    if not isinstance(spec, dict) or spec.get("origin_owner") != "independent-core":
        raise DarwinServiceError("no independently staged Core exists")
    if CLOUDFLARE_SERVICE in services:
        raise DarwinServiceError("cannot unstage Core while Cloudflare connector is installed")
    openai = services.get(TUNNEL_SERVICE)
    if isinstance(openai, dict) and openai.get("connector_owner") == (
        "independent-openai-http"
    ):
        raise DarwinServiceError("cannot unstage Core while OpenAI connector is installed")
    if native_status(HTTP_ORIGIN_SERVICE).get(HTTP_ORIGIN_SERVICE) not in {
        "not-registered", "not-found"
    }:
        raise DarwinServiceError("Core is still registered; stop it explicitly first")
    snapshot = service_home() / _HTTP_ORIGIN_ENV_SNAPSHOT
    _secure_input_file(snapshot, "Core environment snapshot")
    remaining = {key: value for key, value in services.items() if key != HTTP_ORIGIN_SERVICE}
    _atomic_private_write(
        manifest_path(),
        json.dumps(
            {"schema_version": MANIFEST_SCHEMA_VERSION, "services": remaining},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    # The snapshot is generated exclusively for this staged Core. Remove it
    # only after the Core manifest entry is gone; never touch the source env file.
    snapshot.unlink()


def _core_manifest_port() -> int:
    """Validate the existing managed HTTP origin before treating it as Core."""
    manifest = _load_manifest_document()
    spec = manifest["services"].get(HTTP_ORIGIN_SERVICE)
    if not isinstance(spec, dict) or spec.get("origin_owner") != "independent-core":
        raise DarwinServiceError("independent Core is not installed")
    arguments = spec.get("arguments")
    if (
        not isinstance(arguments, list)
        or arguments[:5]
        != ["-m", "serverfs_mcp.cli", "serve", "--transport", "streamable-http"]
        or arguments.count("--http-port") != 1
    ):
        raise DarwinServiceError("independent Core manifest is invalid")
    index = arguments.index("--http-port")
    try:
        port = int(arguments[index + 1])
    except (IndexError, TypeError, ValueError) as exc:
        raise DarwinServiceError("independent Core port is invalid") from exc
    if not 1 <= port <= 65535:
        raise DarwinServiceError("independent Core port is invalid")
    return port


def _independent_core_context(
    config_path: Path,
) -> tuple[int, Path, tuple[Path, ...], dict[str, str]]:
    """Read one canonical Core manifest/config/credential boundary for either connector."""
    from .native_config import load_native_config

    port = _core_manifest_port()
    core = _load_manifest_document()["services"][HTTP_ORIGIN_SERVICE]
    args = core["arguments"]
    if not isinstance(args, list) or args.count("--config") != 1:
        raise DarwinServiceError("independent Core configuration is invalid")
    try:
        raw_config = args[args.index("--config") + 1]
        if not isinstance(raw_config, str) or not Path(raw_config).is_absolute():
            raise ValueError("invalid Core config")
        _reject_symlink_components(Path(raw_config), "Core configuration")
        _reject_symlink_components(config_path, "Connector configuration")
        config = config_path.resolve(strict=True)
        if not config_path.is_absolute() or config != Path(raw_config).resolve(strict=True):
            raise ValueError("different configuration")
    except (IndexError, OSError, ValueError) as exc:
        raise DarwinServiceError(
            "connector must use the same canonical TOML as independent Core"
        ) from exc

    snapshot = _secure_input_file(
        service_home() / _HTTP_ORIGIN_ENV_SNAPSHOT, "Core environment snapshot"
    )
    try:
        environment = json.loads(snapshot.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DarwinServiceError("independent Core environment snapshot is invalid") from exc
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise DarwinServiceError("independent Core environment snapshot is invalid")
    try:
        workdirs, _settings = load_native_config(config)
    except Exception as exc:
        raise DarwinServiceError("independent Core TOML is invalid") from exc
    if not workdirs:
        raise DarwinServiceError("independent Core requires at least one workdir")
    roots: list[Path] = []
    for workdir in workdirs:
        root = Path(workdir.root).expanduser()
        _reject_symlink_components(root, "Core workdir")
        roots.append(root.resolve(strict=True))
    return port, config, tuple(roots), environment


def stage_cloudflare_connector(
    config_path: Path, cloudflared: Path, token_file: Path, hostname: str
) -> dict[str, str]:
    """Stage a Cloudflare Connector bound to one independent Core, without launching it."""
    _validate_private_service_home()
    if not manager_executable().is_file():
        raise DarwinServiceError("installed macOS Service Manager is unavailable")
    _verify_app(installed_app_path())
    _verify_opt_in_agents(installed_app_path())
    current = native_status(CLOUDFLARE_SERVICE).get(CLOUDFLARE_SERVICE)
    if current not in {"not-registered", "not-found"}:
        raise DarwinServiceError("Cloudflare connector is registered; refusing replacement")
    if _managed_job_pid(CLOUDFLARE_SERVICE) is not None:
        raise DarwinServiceError("Cloudflare connector process is still running")
    document = _load_manifest_document()
    services = document["services"]
    if CLOUDFLARE_SERVICE in services:
        raise DarwinServiceError("Cloudflare connector manifest already exists")
    port, config, roots, environment = _independent_core_context(config_path)
    _validated_http_origin_environment(environment, f"{hostname},127.0.0.1:{port}")
    _refuse_unmanaged_cloudflared()
    connector = _cloudflared_spec(cloudflared, token_file, roots, config.parent)
    connector["connector_owner"] = "independent-connector"
    connector["core_port"] = port
    _atomic_private_write(
        manifest_path(),
        json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "services": {**services, CLOUDFLARE_SERVICE: connector},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    return {CLOUDFLARE_SERVICE: "staged-not-registered"}


def unstage_cloudflare_connector() -> None:
    """Remove only an unregistered independent Connector; leave Core running."""
    document = _load_manifest_document()
    services = document["services"]
    connector = services.get(CLOUDFLARE_SERVICE)
    if not isinstance(connector, dict) or connector.get("connector_owner") != (
        "independent-connector"
    ):
        raise DarwinServiceError("no independently staged Cloudflare connector exists")
    if native_status(CLOUDFLARE_SERVICE).get(CLOUDFLARE_SERVICE) not in {
        "not-registered",
        "not-found",
    }:
        raise DarwinServiceError("Cloudflare connector remains registered; stop it first")
    if _managed_job_pid(CLOUDFLARE_SERVICE) is not None:
        raise DarwinServiceError("Cloudflare connector process is still running")
    _atomic_private_write(
        manifest_path(),
        json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "services": {
                    key: value for key, value in services.items() if key != CLOUDFLARE_SERVICE
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )


def stage_openai_http_connector(
    config_path: Path,
    tunnel_client: Path,
    tunnel_id: str,
    control_plane_key_file: Path,
    core_bearer_file: Path,
) -> dict[str, str]:
    """Stage a direct HTTP OpenAI ingress; never launch Core, Bridge or tunnel-client.

    The operator supplies two separate private credentials: control-plane key
    and a file containing the exact Core Authorization header value.
    """
    from .native_tunnel import _TUNNEL_ID

    _validate_private_service_home()
    if not manager_executable().is_file():
        raise DarwinServiceError("installed macOS Service Manager is unavailable")
    _verify_app(installed_app_path())
    if not _TUNNEL_ID.fullmatch(tunnel_id):
        raise DarwinServiceError("invalid OpenAI Tunnel ID")
    if native_status(TUNNEL_SERVICE).get(TUNNEL_SERVICE) not in {
        "not-registered", "not-found"
    } or _managed_job_pid(TUNNEL_SERVICE) is not None:
        raise DarwinServiceError("OpenAI Tunnel is still registered or running")
    if find_unmanaged_tunnel_pids():
        raise DarwinServiceError("unmanaged OpenAI Tunnel process is still running")

    document = _load_manifest_document()
    services = document["services"]
    if TUNNEL_SERVICE in services:
        raise DarwinServiceError("OpenAI Tunnel manifest already exists; refusing replacement")
    port, config, roots, environment = _independent_core_context(config_path)
    hostname_setting = environment.get("SERVERFS_HTTP_ALLOWED_HOSTS", "")
    loopback_suffix = f",127.0.0.1:{port}"
    if not hostname_setting.endswith(loopback_suffix) or hostname_setting.count(",") != 1:
        raise DarwinServiceError("independent Core Host admission is not exact")
    _validated_http_origin_environment(environment, hostname_setting)
    core_key = environment.get("SERVERFS_HTTP_API_KEY")
    if not isinstance(core_key, str) or not core_key.strip():
        raise DarwinServiceError("OpenAI direct HTTP staging requires Core API Key auth")

    checkout = Path(__file__).resolve().parents[2]
    control_plane_key_file = _secure_input_file(
        control_plane_key_file, "OpenAI control-plane key file"
    )
    core_bearer_file = _secure_input_file(core_bearer_file, "Core bearer-header file")
    for path, label in (
        (control_plane_key_file, "OpenAI control-plane key file"),
        (core_bearer_file, "Core bearer-header file"),
    ):
        _validate_private_location(path, (checkout, *roots), label)
    if control_plane_key_file == core_bearer_file:
        raise DarwinServiceError("control-plane and Core credentials must use separate files")
    try:
        control_key = control_plane_key_file.read_text(encoding="utf-8")
        bearer = core_bearer_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DarwinServiceError("private OpenAI credential file cannot be read") from exc
    if (
        not control_key.strip()
        or control_key != control_key.strip()
        or control_key == core_key
        or bearer != f"Bearer {core_key}"
    ):
        raise DarwinServiceError("private OpenAI/Core credential file validation failed")

    tunnel_client = tunnel_client.expanduser()
    if not tunnel_client.is_absolute():
        raise DarwinServiceError("OpenAI tunnel-client must have a fixed absolute path")
    _reject_symlink_components(tunnel_client, "OpenAI tunnel-client")
    try:
        binary_stat = tunnel_client.stat(follow_symlinks=False)
    except OSError as exc:
        raise DarwinServiceError("OpenAI tunnel-client executable is unavailable") from exc
    if (
        not tunnel_client.is_file()
        or binary_stat.st_uid != os.getuid()
        or binary_stat.st_mode & 0o022
        or not os.access(tunnel_client, os.X_OK)
    ):
        raise DarwinServiceError("OpenAI tunnel-client executable is insecure")
    logs = service_log_dir()
    _reject_symlink_components(logs, "service log directory")
    _ensure_private_dir(logs)
    spec = {
        "executable": str(tunnel_client),
        "arguments": [
            "run",
            "--control-plane.tunnel-id",
            tunnel_id,
            "--control-plane.api-key",
            f"file:{control_plane_key_file}",
            "--mcp.server-url",
            f"url=http://127.0.0.1:{port}/mcp",
            "--mcp.extra-headers",
            f"Authorization: file:{core_bearer_file}",
            "--mcp.discovery-extra-headers",
            f"Authorization: file:{core_bearer_file}",
            "--health.listen-addr",
            "127.0.0.1:0",
        ],
        "environment_file": None,
        "working_directory": str(config.parent),
        "stdout_log": str(logs / "openai-connector.stdout.log"),
        "stderr_log": str(logs / "openai-connector.stderr.log"),
        "connector_owner": "independent-openai-http",
        "core_port": port,
    }
    _atomic_private_write(
        manifest_path(),
        json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "services": {**services, TUNNEL_SERVICE: spec},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    return {TUNNEL_SERVICE: "staged-not-registered"}


def unstage_openai_http_connector() -> None:
    """Remove only an unregistered direct-HTTP Connector; preserve both secret files."""
    document = _load_manifest_document()
    services = document["services"]
    spec = services.get(TUNNEL_SERVICE)
    if not isinstance(spec, dict) or spec.get("connector_owner") != (
        "independent-openai-http"
    ):
        raise DarwinServiceError("no independently staged OpenAI HTTP connector exists")
    if native_status(TUNNEL_SERVICE).get(TUNNEL_SERVICE) not in {
        "not-registered", "not-found"
    } or _managed_job_pid(TUNNEL_SERVICE) is not None:
        raise DarwinServiceError("OpenAI Tunnel remains registered or running")
    _atomic_private_write(
        manifest_path(),
        json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "services": {
                    key: value for key, value in services.items() if key != TUNNEL_SERVICE
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )


def core_start() -> dict[str, str]:
    """Start only the persistent HTTP Core, preserving both ingresses and Bridge."""
    port = _core_manifest_port()
    states = _register_and_wait(HTTP_ORIGIN_SERVICE)
    _wait_http_origin(port)
    return states


def core_stop() -> dict[str, str]:
    """Stop only the persistent HTTP Core, never the Bridge or connectors."""
    _core_manifest_port()
    return _unregister_and_wait(HTTP_ORIGIN_SERVICE)


def core_restart() -> dict[str, str]:
    core_stop()
    return core_start()


def cloudflare_connector_start() -> dict[str, str]:
    """Start only the connector; never register or restart its HTTP upstream."""
    manifest = _load_manifest_document()
    if CLOUDFLARE_SERVICE not in manifest["services"]:
        raise DarwinServiceError("Cloudflare connector is not installed")
    _refuse_unmanaged_cloudflared()
    return _register_and_wait(CLOUDFLARE_SERVICE)


def cloudflare_connector_stop() -> dict[str, str]:
    """Stop only cloudflared, preserving the independent HTTP upstream and Bridge."""
    manifest = _load_manifest_document()
    if CLOUDFLARE_SERVICE not in manifest["services"]:
        raise DarwinServiceError("Cloudflare connector is not installed")
    return _unregister_and_wait(CLOUDFLARE_SERVICE)


def cloudflare_connector_restart() -> dict[str, str]:
    cloudflare_connector_stop()
    deadline = time.monotonic() + _CLOUDFLARE_RESTART_WAIT_SECONDS
    while True:
        try:
            return cloudflare_connector_start()
        except DarwinServiceError as exc:
            if str(exc) != "an unmanaged cloudflared tunnel process is already running":
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_CLOUDFLARE_RESTART_POLL_SECONDS, remaining))


def restart_service(service: str) -> dict[str, str]:
    if service not in ALL_SERVICES:
        raise DarwinServiceError("unknown persistent service")
    _unregister_and_wait(service)
    return _register_and_wait(service)


def _refuse_legacy_group_lifecycle(manifest: dict) -> None:
    """Prevent legacy all-component operations on independently owned services."""
    services = manifest.get("services", {})
    if any(
        isinstance(spec, dict)
        and (
            spec.get("origin_owner") == "independent-core"
            or spec.get("connector_owner") in {
                "independent-connector",
                "independent-openai-http",
            }
        )
        for spec in services.values()
    ):
        raise DarwinServiceError(
            "independent v0.25.0 services require explicit --component lifecycle; "
            "legacy grouped service operations are refused"
        )


def start() -> dict[str, str]:
    manifest = _load_manifest_document()
    _refuse_legacy_group_lifecycle(manifest)
    states: dict[str, str] = {}
    for service in ALL_SERVICES:
        if service not in manifest["services"]:
            continue
        states.update(_register_and_wait(service))
    return states


def stop() -> dict[str, str]:
    manifest = _load_manifest_document(optional=True)
    _refuse_legacy_group_lifecycle(manifest)
    services = (
        [service for service in ALL_SERVICES if service in manifest.get("services", ALL_SERVICES)]
        if manifest
        else list(ALL_SERVICES)
    )
    states: dict[str, str] = {}
    for service in services:
        try:
            states.update(_unregister_and_wait(service))
        except DarwinServiceError:
            states[service] = "not-registered"
    return states


def restart() -> dict[str, str]:
    manifest = _load_manifest_document()
    _refuse_legacy_group_lifecycle(manifest)
    services = [service for service in ALL_SERVICES if service in manifest["services"]]
    states: dict[str, str] = {}
    for service in services:
        _unregister_and_wait(service)
    for service in services:
        states.update(_register_and_wait(service))
    return states


def uninstall() -> None:
    _refuse_legacy_group_lifecycle(_load_manifest_document(optional=True))
    try:
        stop()
    finally:
        manifest = _load_manifest_document(optional=True)
        preserved = {
            key: value
            for key, value in manifest.get("services", {}).items()
            if key in _CLOUDFLARE_SERVICES
        }
        if preserved:
            _atomic_private_write(
                manifest_path(),
                json.dumps(
                    {"schema_version": MANIFEST_SCHEMA_VERSION, "services": preserved},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
            )
        else:
            try:
                manifest_path().unlink()
            except FileNotFoundError:
                pass
        for path in (manifest_path(), tunnel_environment_path(), tunnel_proxy_env_path()):
            if path == manifest_path() and preserved:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        app = installed_app_path()
        if app.exists() and not preserved:
            shutil.rmtree(app)


def open_settings() -> None:
    _run_manager("open-settings")


def _load_manifest_document(*, optional: bool = False) -> dict:
    path = manifest_path()
    if not path.is_file():
        if optional:
            return {}
        raise DarwinServiceError("persistent service manifest is not installed")
    try:
        stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DarwinServiceError("persistent service manifest is invalid") from exc
    if path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o777 != 0o600:
        raise DarwinServiceError("persistent service manifest must be current-user mode 0600")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DarwinServiceError("persistent service manifest is invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or not isinstance(document.get("services"), dict)
    ):
        raise DarwinServiceError("persistent service manifest is invalid")
    return document


def status() -> dict[str, object]:
    app = installed_app_path()
    app_installed = app.is_dir()
    result: dict[str, object] = {
        "app_installed": app_installed,
        "app_path": str(app),
        "manifest_installed": manifest_path().is_file(),
        "legacy_bridge_plist": legacy_bridge_plist_path().is_file(),
        "unmanaged_tunnel_pids": find_unmanaged_tunnel_pids(),
        "processes": {service: {"pid": _managed_job_pid(service)} for service in ALL_SERVICES},
        "log_dir": str(service_log_dir()),
        "handover": _handover_status(),
        "build_tools": {
            tool: shutil.which(tool) is not None for tool in ("xcrun", "codesign", "plutil")
        },
    }
    if app_installed:
        try:
            _verify_app(app)
        except DarwinServiceError as exc:
            result["bundle_valid"] = False
            result["bundle_error"] = str(exc)
        else:
            result["bundle_valid"] = True
            result["bundle_error"] = ""
    else:
        result["bundle_valid"] = False
        result["bundle_error"] = "not installed"

    if manager_executable().is_file() and result.get("bundle_valid") is True:
        result["services"] = native_status()
        info = _run_manager("info")
        result["bundle_version"] = info.get("version", "")
    elif app_installed:
        result["services"] = {service: "invalid-bundle" for service in ALL_SERVICES}
        result["bundle_version"] = ""
    else:
        result["services"] = {service: "not-installed" for service in ALL_SERVICES}
        result["bundle_version"] = ""
    return result


def _handover_status() -> dict[str, object]:
    try:
        document = json.loads(handover_result_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        document = {}
    if not isinstance(document, dict):
        document = {}
    try:
        loaded = (
            subprocess.run(
                ["launchctl", "print", _handover_label_target()],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        loaded = None
    return {
        "complete": document.get("complete"),
        "ok": document.get("ok"),
        "detail": document.get("detail"),
        "timestamp": document.get("timestamp"),
        "job_loaded": loaded,
        "plist_exists": handover_plist_path().exists(),
    }

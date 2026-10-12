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
import subprocess
import sys
import time
from pathlib import Path

from .native_tunnel import parse_env_value

APP_NAME = "ServerFSServiceManager.app"
APP_BUNDLE_ID = "com.ntlx.serverfs.service-manager"
APP_VERSION = "0.13.2"
TUNNEL_SERVICE = "tunnel"
BRIDGE_SERVICE = "agent-bridge"
ALL_SERVICES = (TUNNEL_SERVICE, BRIDGE_SERVICE)
BRIDGE_LABEL = "com.ntlx.serverfs.bridge"
LEGACY_BRIDGE_LABEL = "com.ntlx.serverfs.agent-bridge"
_SERVICE_LABELS = {
    TUNNEL_SERVICE: "com.ntlx.serverfs.tunnel",
    BRIDGE_SERVICE: BRIDGE_LABEL,
}
MANIFEST_SCHEMA_VERSION = 1
HANDOVER_LABEL = "com.ntlx.serverfs.v0132-handover"
HANDOVER_SCHEMA_VERSION = 1

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TUNNEL_ENV_KEYS = frozenset({"CONTROL_PLANE_API_KEY", "SERVERFS_DATA_HOME"})
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
        raise DarwinServiceError(
            f"macOS Service Manager refused the lifecycle operation{suffix}"
        )
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
        raise DarwinServiceError(
            "legacy Agent Bridge rollback bootstrap failed" + suffix
        )


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
    except (OSError, subprocess.SubprocessError):
        pass


def write_handover_result(*, ok: bool, detail: str) -> None:
    payload = {
        "schema_version": HANDOVER_SCHEMA_VERSION,
        "ok": ok,
        "detail": detail,
        "timestamp": time.time(),
    }
    _atomic_private_write(
        handover_result_path(),
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def schedule_handover_install(config_path: Path, env_file: Path, root_pids: list[int]) -> None:
    """Run a lifecycle-sensitive install in a one-shot launchd job.

    ``root_pids`` may be empty when only the legacy Agent Bridge needs to be replaced. The
    launchd-owned child is still required so booting out the old Bridge cannot terminate the
    process that is performing the migration.
    """
    _ensure_private_dir(service_home())
    _ensure_private_dir(service_log_dir())
    _bootout_handover_job()
    try:
        handover_result_path().unlink()
    except FileNotFoundError:
        pass
    document = {
        "Label": HANDOVER_LABEL,
        "ProgramArguments": [
            str(Path(sys.executable).absolute()),
            "-m",
            "serverfs_mcp.cli",
            "service",
            "install",
            "--config",
            str(config_path.resolve()),
            "--env-file",
            str(env_file.resolve()),
            "--_handover-child",
            "--_handover-roots",
            ",".join(str(pid) for pid in root_pids),
        ],
        "WorkingDirectory": str(config_path.resolve().parent),
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Background",
        "StandardOutPath": str(service_log_dir() / "handover.stdout.log"),
        "StandardErrorPath": str(service_log_dir() / "handover.stderr.log"),
    }
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
            raise DarwinServiceError("temporary macOS hand-over job could not be bootstrapped")
        started = subprocess.run(
            ["launchctl", "kickstart", _handover_label_target()],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if started.returncode != 0:
            _bootout_handover_job()
            raise DarwinServiceError("temporary macOS hand-over job could not be started")
    except (OSError, subprocess.SubprocessError) as exc:
        _bootout_handover_job()
        raise DarwinServiceError(
            f"temporary macOS hand-over job failed ({type(exc).__name__})"
        ) from exc


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
    return _register(service)


def unregister_service(service: str) -> dict[str, str]:
    if service not in ALL_SERVICES:
        raise DarwinServiceError("unknown persistent service")
    return _unregister_and_wait(service)


def restart_service(service: str) -> dict[str, str]:
    unregister_service(service)
    return register_service(service)


def start() -> dict[str, str]:
    manifest = _load_manifest_document()
    states: dict[str, str] = {}
    for service in manifest["services"]:
        states.update(_register(service))
    return states


def stop() -> dict[str, str]:
    manifest = _load_manifest_document(optional=True)
    services = list(manifest.get("services", ALL_SERVICES)) if manifest else list(ALL_SERVICES)
    states: dict[str, str] = {}
    for service in services:
        try:
            states.update(_unregister_and_wait(service))
        except DarwinServiceError:
            states[service] = "not-registered"
    return states


def restart() -> dict[str, str]:
    stop()
    return start()


def uninstall() -> None:
    try:
        stop()
    finally:
        for path in (manifest_path(), tunnel_environment_path(), tunnel_proxy_env_path()):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        app = installed_app_path()
        if app.exists():
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

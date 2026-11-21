"""Independent Windows Agent Bridge watchdog/runner (v0.25.0 W1b-2, source only).

This is NOT installed, registered or wired into the existing ServerFS CLI.
Its Task Scheduler owner is the fixed "ServerFS Agent Bridge" task, never a
Tunnel or Cloudflare task. The watchdog owns an anonymous kill-on-close Job
for its *own* runner; the runner creates the named single-user Bridge Job
through windows_bridge_owner. No Core/Connector process belongs to either job.

An authenticated Bridge IPC ping alone is NOT a generation health proof while
a legacy same-SID Bridge is live. The initial watchdog therefore supervises
only its own runner process identity and reported exit code. Generation-bound
IPC liveness requires a later protocol/owner proof, not a naked SID pipe ping.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import windows_bridge_task_spec as contract
from .windows_bridge_owner import EXIT_REFUSED
from .windows_bridge_owner import run as run_bridge_owner

STATE_SCHEMA_VERSION = 1
INITIAL_RESTART_DELAY_SECONDS = 5.0
MAX_RESTART_DELAY_SECONDS = 60.0
HEALTHY_RESET_SECONDS = 300.0
WATCHDOG_INITIAL_RESTART_DELAY_SECONDS = 2.0
WATCHDOG_MAX_RESTART_DELAY_SECONDS = 30.0
WATCHDOG_HEALTHY_RESET_SECONDS = 300.0
_REDACTED_RUNTIME_PREFIXES = (
    "CONTROL_PLANE_",
    "TUNNEL_CLIENT_",
    "OPENAI_",
    "MCP_",
    "SERVERFS_PROXY_",
)
_PROXY_ENV_NAMES = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
_UNSAFE_PYTHON_ENV = {"pythonpath", "pythonhome", "pythonstartup", "pythoninspect"}


class BridgeServiceError(Exception):
    """Bridge-only lifecycle refusal with an operator-safe error."""


def _ensure_windows() -> None:
    if sys.platform != "win32" or os.name != "nt":
        raise BridgeServiceError("the independent Bridge service requires Windows")


def service_home() -> Path:
    """Per-user identity from Win32 Known Folder, not a forgeable env var."""
    from .native_lifecycle import window_local_app_data

    return window_local_app_data() / "ServerFS" / "service-manager" / "windows" / "agent-bridge"


def manifest_path() -> Path:
    return service_home() / "service.json"


def authored_task_path() -> Path:
    return service_home() / "task.xml"


def state_path() -> Path:
    return service_home() / "state.json"


def watchdog_state_path() -> Path:
    return service_home() / "watchdog.json"


def log_dir() -> Path:
    return service_home() / "logs"


def _runtime_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Remove Tunnel and ambient proxy secrets without dropping Agent policy env.

    The Bridge owner and Bridge process apply their own deeper sanitization.
    No Control Plane credential may cross the watchdog -> runner boundary.
    """
    original = dict(os.environ if source is None else source)
    return {
        key: value
        for key, value in original.items()
        if not key.upper().startswith(_REDACTED_RUNTIME_PREFIXES)
        and key.lower() not in _PROXY_ENV_NAMES
        and key.lower() not in _UNSAFE_PYTHON_ENV
        and key.upper() != "SERVERFS_BRIDGE_PYTHON"
    }


def _next_restart_delay(
    runtime_seconds: float, previous_delay: float, *, watchdog: bool = False
) -> float:
    """Bounded exponential recovery; a healthy generation resets backoff."""
    initial = WATCHDOG_INITIAL_RESTART_DELAY_SECONDS if watchdog else INITIAL_RESTART_DELAY_SECONDS
    maximum = WATCHDOG_MAX_RESTART_DELAY_SECONDS if watchdog else MAX_RESTART_DELAY_SECONDS
    reset = WATCHDOG_HEALTHY_RESET_SECONDS if watchdog else HEALTHY_RESET_SECONDS
    if runtime_seconds >= reset:
        return initial
    return min(maximum, max(initial, previous_delay * 2))


def _canonical_match(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def _read_manifest() -> dict[str, Any]:
    from .windows_core_control import _check_private, _current_sid

    try:
        _check_private(manifest_path(), directory=False, sid=_current_sid())
        manifest = json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeServiceError("Bridge service manifest is unavailable or invalid") from exc
    ok, _ = contract.validate_manifest(manifest)
    if not ok:
        raise BridgeServiceError("Bridge service manifest violates its fixed contract")
    return manifest


def _verified_bridge_interpreter(manifest: dict[str, Any]) -> Path:
    """Verify the separate Bridge+SDK interpreter is privately owned and pinned.

    No environment-derived path can select Bridge code. The protected sibling
    environment is distinct from the owner/Product+Native environment.
    """
    from .native_lifecycle import window_local_app_data
    from .windows_core_control import _check_private

    root = window_local_app_data() / "ServerFS" / "bridge-runtime-env"
    executable = root / "Scripts" / "python.exe"
    if not _canonical_match(manifest["bridge_executable"], executable) or _canonical_match(
        manifest["task_executable"], executable
    ):
        raise BridgeServiceError("Bridge runtime interpreter is outside its pinned scope")
    try:
        for directory in (
            root,
            root / "Scripts",
            root / "Lib",
            root / "Lib" / "site-packages",
            root / "Lib" / "site-packages" / "serverfs_agent_bridge",
        ):
            _check_private(directory, directory=True, sid=manifest["user_sid"])
        _check_private(executable, directory=False, sid=manifest["user_sid"])
    except Exception as exc:  # noqa: BLE001 - never print a private path or ACL
        raise BridgeServiceError("Bridge runtime private installation is unverified") from exc
    return executable


def _validate_staged_artifacts(config: Path) -> dict[str, Any]:
    """Refuse a different user, config, task or wheel before starting a process."""
    _ensure_windows()
    manifest = _read_manifest()
    from .native_endpoint import current_user_sid

    if manifest["user_sid"] != current_user_sid():
        raise BridgeServiceError("Bridge service manifest belongs to another user")
    if not _canonical_match(manifest["config_path"], config.resolve(strict=True)):
        raise BridgeServiceError("Bridge service policy path does not match installed manifest")
    executable = Path(sys.executable).resolve()
    expected = executable.with_name("pythonw.exe")
    if not _canonical_match(manifest["task_executable"], expected):
        raise BridgeServiceError("Bridge service interpreter does not match installed manifest")
    _verified_bridge_interpreter(manifest)
    try:
        from .windows_core_control import _check_private

        _check_private(authored_task_path(), directory=False, sid=manifest["user_sid"])
        authored = authored_task_path().read_bytes()
    except Exception as exc:  # noqa: BLE001 - do not expose private filesystem data
        raise BridgeServiceError("Bridge authored task XML is unavailable") from exc
    ok, _ = contract.validate_authored_task_xml(authored, manifest)
    if not ok:
        raise BridgeServiceError("Bridge authored task XML contract is invalid")
    return manifest


def _registered_task_xml() -> str:
    """Query only the fixed Bridge task; no fallback to legacy Task Scheduler owners."""
    from .windows_service import _decode_process_output, _schtasks_executable

    try:
        result = subprocess.run(
            [
                str(_schtasks_executable()),
                "/Query",
                "/TN",
                contract.TASK_NAME,
                "/XML",
                "/HResult",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeServiceError("Bridge task query is unavailable") from exc
    if result.returncode != 0:
        raise BridgeServiceError("Bridge task is not registered or cannot be queried")
    text = _decode_process_output(result.stdout).lstrip("\ufeff\x00 \t\r\n")
    if not text:
        raise BridgeServiceError("Bridge task definition is empty")
    return text


def _require_registered_task(config: Path) -> dict[str, Any]:
    """A staged manifest alone does not authorize a Bridge service generation."""
    manifest = _validate_staged_artifacts(config)
    task_xml = _registered_task_xml()
    ok, _ = contract.validate_registered_task(task_xml, manifest)
    if not ok:
        raise BridgeServiceError("registered Bridge task has an unexpected contract")
    # Task Scheduler's normalized disabled state must be treated as authoritative.
    from xml.etree import ElementTree as ET

    root = ET.fromstring(task_xml)
    enabled = root.findtext(
        "./t:Settings/t:Enabled", default="true", namespaces={"t": contract.TASK_XML_NAMESPACE}
    )
    if enabled.strip().lower() == "false":
        raise BridgeServiceError("Bridge task is disabled; refusing an independent generation")
    return manifest


def _atomic_write_state(path: Path, data: dict[str, object]) -> None:
    """Write no secrets; never share the legacy task state files."""
    payload = json.dumps(
        {"schema_version": STATE_SCHEMA_VERSION, **data}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    from .windows_core_control import _check_private, _current_sid
    from .windows_private_artifacts import protect_new_file

    sid = _current_sid()
    try:
        # The owner must use an already provisioned private directory.
        # Creating one here would inherit an unaudited ambient DACL.
        _check_private(path.parent, directory=True, sid=sid)
        if path.exists() or path.is_symlink():
            _check_private(path, directory=False, sid=sid)
        with tmp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        protect_new_file(tmp, sid)
        _check_private(tmp, directory=False, sid=sid)
        os.replace(tmp, path)
        _check_private(path, directory=False, sid=sid)
    except Exception as exc:  # noqa: BLE001 - fixed redacted runtime errors
        raise BridgeServiceError("Bridge service private state update failed") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _process_state(prefix: str, pid: int) -> dict[str, object]:
    """Record creation-time identity, not a PID-only claim."""
    from .windows_process import process_identity

    identity = process_identity(pid)
    if identity is None:
        raise BridgeServiceError("Bridge service process identity is unavailable")
    return {
        f"{prefix}_pid": identity.pid,
        f"{prefix}_image_path": str(identity.image_path),
        f"{prefix}_creation_time": identity.creation_time,
    }


def _private_log_path(filename: str) -> Path:
    """Require pre-provisioned protected log storage, never create it implicitly."""
    if filename not in ("lifecycle.log", "runner.stderr.log"):
        raise BridgeServiceError("Bridge log filename is invalid")
    from .windows_core_control import _check_private, _current_sid

    sid = _current_sid()
    _check_private(log_dir(), directory=True, sid=sid)
    path = log_dir() / filename
    _check_private(path, directory=False, sid=sid)
    return path


def _append_event(name: str, **fields: object) -> None:
    """Keep logs to fixed event identifiers and numeric fields."""
    try:
        with _private_log_path("lifecycle.log").open("a", encoding="utf-8") as output:
            output.write(
                json.dumps(
                    {"event": name, "timestamp": time.time(), **fields},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    except OSError as exc:
        raise BridgeServiceError("Bridge lifecycle log is unavailable") from exc


def run_owned_generation(config: Path) -> int:
    """One owned generation; never launches a Connector, Core or nested runner."""
    manifest = _require_registered_task(config)
    # The private manifest has already passed owner, DACL and interpreter
    # validation. Never fall back to ambient SERVERFS_BRIDGE_PYTHON.
    return run_bridge_owner(config.resolve(), bridge_python=Path(manifest["bridge_executable"]))


def _runner_arguments(config: Path) -> list[str]:
    return [
        "-I",
        "-m",
        "serverfs_mcp.windows_bridge_service",
        "_run",
        "--config",
        str(config.resolve()),
    ]


def _runner_executable() -> Path:
    interpreter = Path(sys.executable).resolve()
    expected = interpreter.with_name("pythonw.exe")
    if not expected.is_file():
        raise BridgeServiceError("Bridge service pythonw.exe is unavailable")
    return expected


def run_watchdog(config: Path) -> int:
    """Component-only persistent recovery with a kill-on-close runner Job.

    The Task Scheduler watchdog owns exactly one runner at a time. The runner
    calls W1a Bridge owner and owns the *named* Bridge Job. Ending this task
    cannot end an OpenAI Tunnel, Core or Cloudflare component. Supervision
    observes the actual runner exit, not a misleading legacy SID-pipe ping.
    """
    _require_registered_task(config)
    from .windows_job import WindowsJob

    watchdog_identity = _process_state("watchdog", os.getpid())
    started_at = time.time()
    # The first write invalidates the installer-created "never started"
    # marker BEFORE opening a Job or launching any Runner. Even if a later
    # pre-spawn exception occurs, offline uninstall cannot misclassify it.
    _atomic_write_state(
        watchdog_state_path(),
        {
            "component": "agent-bridge",
            "stage": "owner-started",
            **watchdog_identity,
            "watchdog_started_at": started_at,
            "runner_restart_count": 0,
            "managed_runner_pid": None,
        },
    )
    attempts = 0
    delay = WATCHDOG_INITIAL_RESTART_DELAY_SECONDS
    while True:
        attempts += 1
        started = time.time()
        monotonic_start = time.monotonic()
        exit_code = EXIT_REFUSED
        job = WindowsJob()
        process: subprocess.Popen[bytes] | None = None
        logfile = None
        error_class = ""
        try:
            logfile = _private_log_path("runner.stderr.log").open("ab", buffering=0)
            job.open()
            # Suspended launch prevents a Python venv launcher from creating
            # a real Bridge before the watchdog's own Job has containment.
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_SUSPENDED", 0x00000004
            )
            process = subprocess.Popen(
                [str(_runner_executable()), *_runner_arguments(config)],
                # Never make an exposed configuration directory importable as cwd.
                cwd=_runner_executable().parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=logfile,
                env=_runtime_environment(),
                creationflags=flags,
            )
            job.assign(process)
            job.resume(process)
            _atomic_write_state(
                watchdog_state_path(),
                {
                    "component": "agent-bridge",
                    "stage": "owner-started",
                    **watchdog_identity,
                    "watchdog_started_at": started_at,
                    "runner_restart_count": attempts,
                    **_process_state("managed_runner", process.pid),
                    "last_runner_started_at": started,
                },
            )
            _append_event("bridge_runner_started", generation=attempts, pid=process.pid)
            exit_code = process.wait()
        except Exception as exc:  # noqa: BLE001 - one failed generation must not leak a child
            error_class = type(exc).__name__
            # Popen can succeed before Job assignment fails. At that point the
            # suspended child is not contained, so Job close is not sufficient.
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                    raise BridgeServiceError(
                        "Bridge watchdog cannot prove failed runner termination"
                    ) from cleanup_error
        finally:
            try:
                # Never leave a possibly live runner/Bridge outside containment.
                job.close()
            except Exception as exc:  # noqa: BLE001 - fail closed, never start a new generation
                raise BridgeServiceError("Bridge watchdog could not close the runner Job") from exc
            finally:
                if logfile is not None:
                    logfile.close()

        runtime = max(0.0, time.monotonic() - monotonic_start)
        _atomic_write_state(
            watchdog_state_path(),
            {
                "component": "agent-bridge",
                "stage": "owner-started",
                **watchdog_identity,
                "watchdog_started_at": started_at,
                "runner_restart_count": attempts,
                "managed_runner_pid": None,
                "managed_runner_image_path": None,
                "managed_runner_creation_time": None,
                "last_runner_exit_at": time.time(),
                "last_runner_exit_code": exit_code,
                "last_runner_runtime_seconds": round(runtime, 3),
                "next_runner_restart_delay_seconds": delay,
            },
        )
        _append_event(
            "bridge_runner_exited",
            generation=attempts,
            exit_code=exit_code,
            error_class=error_class,
        )
        time.sleep(delay)
        delay = _next_restart_delay(runtime, delay, watchdog=True)


def _read_state(path: Path) -> dict[str, Any]:
    from .windows_core_control import _check_private, _current_sid

    try:
        _check_private(path, directory=False, sid=_current_sid())
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an untrusted state is not ownership evidence
        return {}
    if not isinstance(value, dict) or value.get("schema_version") != STATE_SCHEMA_VERSION:
        return {}
    return value


def _identity_alive(state: dict[str, Any], prefix: str) -> bool:
    """Never equate a re-used PID with the recorded Bridge component process."""
    from .windows_process import ProcessIdentity, same_process

    pid = state.get(f"{prefix}_pid")
    image = state.get(f"{prefix}_image_path")
    started = state.get(f"{prefix}_creation_time")
    if (
        type(pid) is not int
        or pid <= 0
        or not isinstance(image, str)
        or not image
        or type(started) is not int
        or started <= 0
    ):
        return False
    return same_process(ProcessIdentity(pid=pid, image_path=Path(image), creation_time=started))


def status() -> dict[str, Any]:
    """Component-only read-only status; never touches default Tunnel task state."""
    _ensure_windows()
    manifest: dict[str, Any] | None = None
    manifest_error = ""
    try:
        manifest = _read_manifest()
    except BridgeServiceError as exc:
        manifest_error = str(exc)

    task_xml: str | None = None
    task_error = ""
    registered: bool | None = None
    try:
        task_xml = _registered_task_xml()
        registered = True
    except BridgeServiceError as exc:
        task_error = str(exc)

    valid = False
    enabled: bool | None = None
    if manifest is not None and task_xml is not None:
        valid, task_error = contract.validate_registered_task(task_xml, manifest)
    elif task_xml is not None:
        task_error = "registered Bridge task has no valid component manifest"

    if task_xml is not None:
        try:
            from xml.etree import ElementTree as ET

            root = ET.fromstring(task_xml)
            raw = root.findtext(
                "./t:Settings/t:Enabled",
                default="true",
                namespaces={"t": contract.TASK_XML_NAMESPACE},
            )
            enabled = raw.strip().lower() != "false"
        except ET.ParseError:
            task_error = "registered Bridge task XML is invalid"

    state = _read_state(watchdog_state_path())
    watchdog_alive = _identity_alive(state, "watchdog")
    runner_alive = _identity_alive(state, "managed_runner")
    return {
        "component": "agent-bridge",
        "backend": "windows-task-scheduler",
        "task_name": contract.TASK_NAME,
        "registered": registered,
        "enabled": enabled,
        "task_valid": valid,
        "task_error": task_error,
        "manifest_installed": manifest is not None,
        "manifest_error": manifest_error,
        "watchdog_alive": watchdog_alive,
        "watchdog_pid": state.get("watchdog_pid") if watchdog_alive else None,
        "runner_alive": runner_alive,
        "runner_pid": state.get("managed_runner_pid") if runner_alive else None,
        "runner_restart_count": state.get("runner_restart_count"),
        "last_runner_exit_code": state.get("last_runner_exit_code"),
        "last_runner_runtime_seconds": state.get("last_runner_runtime_seconds"),
        # A same-SID named pipe may be the *legacy* Bridge. Do not attach to
        # it or claim this generation is authenticated/ready from this query.
        "bridge_generation_readiness": "not_verified",
        "state_path": str(watchdog_state_path()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="serverfs-bridge-service",
        description="Internal independent Windows Bridge service runtime.",
    )
    parser.add_argument("_mode", choices=("_watchdog", "_run"))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        _ensure_windows()
        if args._mode == "_watchdog":
            return run_watchdog(args.config)
        return run_owned_generation(args.config)
    except BridgeServiceError as exc:
        print(f"serverfs-bridge-service: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - redact unexpected native errors
        print(f"serverfs-bridge-service: operation failed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())

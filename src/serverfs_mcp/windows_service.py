"""Current-user persistent lifecycle for native Windows ServerFS (v0.14.0).

Windows persistence is deliberately implemented with Task Scheduler 2.0 rather
than the Service Control Manager.  The project-wide deployment invariant is that
install, runtime, maintenance and removal must all work from the current user's
ordinary token: no administrator membership, UAC elevation, machine service,
HKLM registration or stored account password.

The scheduled task is therefore bound to the current SID with
``InteractiveToken`` + ``LeastPrivilege`` and a per-user logon trigger. Task
Scheduler owns a small, windowless ServerFS watchdog. The watchdog contains one
managed runner in a kill-on-close Job Object and restarts that runner with a
short bounded backoff; the runner in turn owns one Tunnel generation at a time
and restarts Tunnel generations independently. Task Scheduler's native
``RestartOnFailure`` is the final safety net for the watchdog itself, not the
primary recovery loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from .native_tunnel import (
    NativeTunnelError,
    native_tunnel_instance_lock,
    parse_env_value,
    run_native_tunnel,
)

BACKEND = "windows-task-scheduler"
TASK_NAME = "ServerFS Native Tunnel"
TASK_URI = f"\\{TASK_NAME}"
TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
MANIFEST_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
HANDOVER_SCHEMA_VERSION = 1
HANDOVER_INITIATOR_WAIT_SECONDS = 30.0
SCHEDULER_REPETITION_INTERVAL = "PT1M"
SCHEDULER_RESTART_INTERVAL = "PT1M"
SCHEDULER_RESTART_COUNT = 255
WATCHDOG_INITIAL_RESTART_DELAY_SECONDS = 2.0
WATCHDOG_MAX_RESTART_DELAY_SECONDS = 30.0
WATCHDOG_HEALTHY_RESET_SECONDS = 300.0
INITIAL_RESTART_DELAY_SECONDS = 5.0
MAX_RESTART_DELAY_SECONDS = 60.0
HEALTHY_RUN_RESET_SECONDS = 300.0
TASK_QUERY_ATTEMPTS = 3
TASK_QUERY_RETRY_SECONDS = 0.1
SCHEDULER_END_GRACE_SECONDS = 5.0
_TASK_NOT_FOUND_HRESULTS = frozenset({0x80070002, 0x80070003})

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RUNTIME_ENV_KEYS = frozenset(
    {
        "SERVERFS_AGENT_PROXY_URL",
        "SERVERFS_AGENT_NO_PROXY",
        "SERVERFS_BRIDGE_PYTHON",
        "SERVERFS_DATA_HOME",
    }
)


class WindowsServiceError(Exception):
    """Operator-facing Windows lifecycle failure, redacted by construction."""


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise WindowsServiceError("Windows persistent service management requires Windows")


def _local_app_data() -> Path:
    if os.name != "nt":  # test/import fallback only
        return Path.home() / "AppData" / "Local"
    from .native_lifecycle import window_local_app_data

    return window_local_app_data()


def service_home() -> Path:
    return _local_app_data() / "ServerFS" / "service-manager" / "windows"


def manifest_path() -> Path:
    return service_home() / "service.json"


def task_xml_path() -> Path:
    return service_home() / "task.xml"


def state_path() -> Path:
    return service_home() / "state.json"


def watchdog_state_path() -> Path:
    return service_home() / "watchdog.json"


def handover_path() -> Path:
    return service_home() / "handover.json"


def service_log_dir() -> Path:
    return service_home() / "logs"


def lifecycle_log_path() -> Path:
    return service_log_dir() / "lifecycle.log"


def tunnel_stdout_path() -> Path:
    return service_log_dir() / "tunnel.stdout.log"


def tunnel_stderr_path() -> Path:
    return service_log_dir() / "tunnel.stderr.log"


def runner_stderr_path() -> Path:
    return service_log_dir() / "runner.stderr.log"


def default_api_key_path() -> Path:
    return Path.home() / ".config" / "serverfs" / "api-key"


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WindowsServiceError(
            f"Windows service directory could not be created ({type(exc).__name__})"
        ) from exc
    if not path.is_dir():
        raise WindowsServiceError("Windows service path is not a directory")


def _atomic_write(path: Path, payload: bytes) -> None:
    _ensure_directory(path.parent)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}"
    try:
        with open(temporary, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise WindowsServiceError(
            f"Windows service state could not be written ({type(exc).__name__})"
        ) from exc


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise WindowsServiceError("service env file does not exist")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise WindowsServiceError("service env file cannot be read") from exc
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
            raise WindowsServiceError(f"invalid env assignment at line {line_number}")
        if key in values:
            raise WindowsServiceError(f"duplicate {key} at line {line_number}")
        try:
            values[key] = parse_env_value(raw.strip(), key, line_number)
        except ValueError as exc:
            raise WindowsServiceError(str(exc)) from exc
    return values


def _tunnel_id(values: dict[str, str]) -> str:
    value = values.get("CONTROL_PLANE_TUNNEL_ID", "").strip()
    if not value:
        raise WindowsServiceError(
            "CONTROL_PLANE_TUNNEL_ID is required for the persistent Windows Tunnel"
        )
    return value


def _persistent_runtime_root() -> Path:
    """Return the stable current-user runtime owned by the Windows deployment."""
    return (_local_app_data() / "ServerFS" / "connector-env").resolve()


def _validate_persistent_runtime_location() -> None:
    """Refuse to bind Task Scheduler to a disposable development environment."""
    active = Path(sys.prefix).resolve()
    expected = _persistent_runtime_root()
    if os.path.normcase(str(active)) != os.path.normcase(str(expected)):
        raise WindowsServiceError(
            "persistent Windows service install must run from the current-user connector-env"
        )


def _validate_runtime_backend() -> None:
    """Refuse to persist a Windows interpreter that cannot serve native workdirs."""
    from importlib import import_module
    from importlib.metadata import PackageNotFoundError, version

    from . import SERVER_VERSION

    try:
        import_module("serverfs_windows_native")
        native_version = version("serverfs-windows-native")
    except (ImportError, PackageNotFoundError) as exc:
        raise WindowsServiceError(
            "serverfs-windows-native is not installed in the active ServerFS interpreter"
        ) from exc
    if native_version != SERVER_VERSION:
        raise WindowsServiceError(
            "ServerFS/native version mismatch in the active interpreter "
            f"(serverfs-mcp {SERVER_VERSION}, serverfs-windows-native {native_version})"
        )


def _current_user_sid() -> str:
    from .native_endpoint import current_user_sid

    try:
        return current_user_sid()
    except Exception as exc:  # noqa: BLE001 - normalize platform failure
        raise WindowsServiceError(
            f"current Windows user SID is unavailable ({type(exc).__name__})"
        ) from exc


def _runner_executable() -> Path:
    executable = Path(sys.executable).resolve()
    candidate = executable.with_name("pythonw.exe")
    if not candidate.is_file():
        raise WindowsServiceError(
            "pythonw.exe is unavailable beside the active ServerFS interpreter"
        )
    return candidate


def _runner_argv(config_path: Path, env_file: Path) -> list[str]:
    return [
        "-m",
        "serverfs_mcp.cli",
        "service",
        "_run",
        "--config",
        str(config_path.resolve()),
        "--env-file",
        str(env_file.resolve()),
    ]


def _watchdog_argv(config_path: Path, env_file: Path) -> list[str]:
    return [
        "-m",
        "serverfs_mcp.cli",
        "service",
        "_watchdog",
        "--config",
        str(config_path.resolve()),
        "--env-file",
        str(env_file.resolve()),
    ]


def _qname(local_name: str) -> str:
    return f"{{{TASK_XML_NAMESPACE}}}{local_name}"


def _child(parent: ET.Element, name: str, text: str | None = None, **attributes: str) -> ET.Element:
    element = ET.SubElement(parent, _qname(name), attributes)
    if text is not None:
        element.text = text
    return element


def _task_start_boundary() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def build_task_xml(
    *,
    user_sid: str,
    task_executable: Path,
    config_path: Path,
    env_file: Path,
    enabled: bool = True,
    start_boundary: str | None = None,
) -> bytes:
    """Render the v0.14.0 Task Scheduler contract without secret material."""
    ET.register_namespace("", TASK_XML_NAMESPACE)
    task = ET.Element(_qname("Task"), {"version": "1.4"})

    registration = _child(task, "RegistrationInfo")
    _child(registration, "Author", user_sid)
    _child(
        registration,
        "Description",
        "ServerFS native persistent Tunnel for the current Windows user (v0.14.0)",
    )
    _child(registration, "URI", TASK_URI)

    triggers = _child(task, "Triggers")
    logon = _child(triggers, "LogonTrigger")
    _child(logon, "Enabled", "true")
    _child(logon, "UserId", user_sid)

    heartbeat = _child(triggers, "TimeTrigger")
    heartbeat_repetition = _child(heartbeat, "Repetition")
    _child(heartbeat_repetition, "Interval", SCHEDULER_REPETITION_INTERVAL)
    # No Duration means indefinite repetition in the Task Scheduler schema.
    # MultipleInstancesPolicy=IgnoreNew turns this into an OS ownership
    # heartbeat: a healthy watchdog remains the single instance, while a
    # missing watchdog is recreated by the next native trigger.
    _child(heartbeat_repetition, "StopAtDurationEnd", "false")
    _child(heartbeat, "StartBoundary", start_boundary or _task_start_boundary())
    _child(heartbeat, "Enabled", "true")

    principals = _child(task, "Principals")
    principal = _child(principals, "Principal", None, id="Author")
    _child(principal, "UserId", user_sid)
    _child(principal, "LogonType", "InteractiveToken")
    _child(principal, "RunLevel", "LeastPrivilege")

    settings = _child(task, "Settings")
    _child(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _child(settings, "DisallowStartIfOnBatteries", "false")
    _child(settings, "StopIfGoingOnBatteries", "false")
    _child(settings, "AllowHardTerminate", "true")
    _child(settings, "StartWhenAvailable", "true")
    _child(settings, "RunOnlyIfNetworkAvailable", "false")
    _child(settings, "AllowStartOnDemand", "true")
    _child(settings, "Enabled", "true" if enabled else "false")
    _child(settings, "Hidden", "false")
    _child(settings, "RunOnlyIfIdle", "false")
    _child(settings, "WakeToRun", "false")
    _child(settings, "ExecutionTimeLimit", "PT0S")
    _child(settings, "Priority", "7")
    restart = _child(settings, "RestartOnFailure")
    _child(restart, "Interval", SCHEDULER_RESTART_INTERVAL)
    _child(restart, "Count", str(SCHEDULER_RESTART_COUNT))

    actions = _child(task, "Actions", None, Context="Author")
    action = _child(actions, "Exec")
    _child(action, "Command", str(task_executable.resolve()))
    _child(action, "Arguments", subprocess.list2cmdline(_watchdog_argv(config_path, env_file)))
    _child(action, "WorkingDirectory", str(config_path.resolve().parent))

    xml = ET.tostring(task, encoding="unicode", xml_declaration=False)
    return ('<?xml version="1.0" encoding="UTF-16"?>\r\n' + xml).encode("utf-16")


def _system_directory() -> Path:
    if os.name != "nt":  # test/import fallback only
        return Path("C:/Windows/System32")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise WindowsServiceError("Windows system directory is unavailable")
    return Path(buffer.value)


def _schtasks_executable() -> Path:
    executable = _system_directory() / "schtasks.exe"
    if not executable.is_file():
        raise WindowsServiceError("Windows Task Scheduler CLI is unavailable")
    return executable


def _decode_process_output(payload: bytes) -> str:
    if not payload:
        return ""
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16", errors="replace")
    for encoding in ("utf-8", "mbcs" if os.name == "nt" else "utf-8"):
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _run_schtasks(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            [str(_schtasks_executable()), *args],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WindowsServiceError(
            f"Windows Task Scheduler command could not run ({type(exc).__name__})"
        ) from exc
    if check and completed.returncode != 0:
        raise WindowsServiceError(
            f"Windows Task Scheduler operation failed (exit={completed.returncode})"
        )
    return completed


def _query_task_xml(*, optional: bool = False) -> str | None:
    """Return the registered Task XML without conflating absence with query failure.

    ``schtasks /Query`` accepts ``/XML`` as a flag; it has no XML-type argument.
    ``/HResult`` makes failures machine-readable so localized stderr never decides
    whether a task is absent.  A short bounded retry absorbs Task Scheduler's
    registration/start transition without turning a transient query error into
    ``registered=false``.
    """
    last_hresult = 0
    for attempt in range(TASK_QUERY_ATTEMPTS):
        completed = _run_schtasks(
            "/Query",
            "/TN",
            TASK_NAME,
            "/XML",
            "/HResult",
            check=False,
        )
        if completed.returncode == 0:
            output = _decode_process_output(completed.stdout).lstrip("\ufeff\x00 \t\r\n")
            if not output:
                raise WindowsServiceError(
                    "Windows Task Scheduler returned an empty task definition"
                )
            return output

        last_hresult = completed.returncode & 0xFFFFFFFF
        if attempt + 1 < TASK_QUERY_ATTEMPTS:
            time.sleep(TASK_QUERY_RETRY_SECONDS)

    if last_hresult in _TASK_NOT_FOUND_HRESULTS:
        if optional:
            return None
        raise WindowsServiceError("Windows persistent task is not registered")
    raise WindowsServiceError(f"Windows Task Scheduler query failed (hresult=0x{last_hresult:08X})")


def _manifest_document() -> dict[str, Any]:
    path = manifest_path()
    if not path.is_file():
        raise WindowsServiceError("Windows persistent service manifest is not installed")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WindowsServiceError("Windows persistent service manifest is invalid") from exc
    if not isinstance(document, dict) or document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise WindowsServiceError("Windows persistent service manifest is invalid")
    return document


def _write_manifest(
    *,
    config_path: Path,
    env_file: Path,
    user_sid: str,
    task_executable: Path,
    task_xml: bytes,
) -> None:
    document = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "backend": BACKEND,
        "task_name": TASK_NAME,
        "task_uri": TASK_URI,
        "user_sid": user_sid,
        "config_path": str(config_path.resolve()),
        "env_file": str(env_file.resolve()),
        "task_executable": str(task_executable.resolve()),
        "task_arguments": _watchdog_argv(config_path, env_file),
        "task_xml_sha256": hashlib.sha256(task_xml).hexdigest(),
    }
    _atomic_write(
        manifest_path(),
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def _xml_text(root: ET.Element, path: str) -> str:
    element = root.find(path, {"t": TASK_XML_NAMESPACE})
    return "" if element is None or element.text is None else element.text.strip()


def _identity_matches_sid(identity: str, expected_sid: str) -> bool:
    """Whether a Task Scheduler user identifier names the expected Windows SID.

    Task Scheduler is allowed to normalize a LogonTrigger ``UserId`` from a SID
    into ``DOMAIN\\user`` when it persists the task.  Compare security identity,
    not spelling: a different account name must never pass merely because the
    scheduler chose a different representation.
    """
    candidate = identity.strip()
    expected = expected_sid.strip()
    if not candidate or not expected:
        return False
    if candidate.casefold() == expected.casefold():
        return True
    if os.name != "nt":
        return False

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.LookupAccountNameW.restype = wintypes.BOOL
    advapi32.LookupAccountNameW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_uint),
    ]
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    expected_ptr = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(expected, ctypes.byref(expected_ptr)):
        return False
    try:
        sid_size = wintypes.DWORD(0)
        domain_size = wintypes.DWORD(0)
        sid_use = ctypes.c_uint(0)
        ctypes.set_last_error(0)
        advapi32.LookupAccountNameW(
            None,
            candidate,
            None,
            ctypes.byref(sid_size),
            None,
            ctypes.byref(domain_size),
            ctypes.byref(sid_use),
        )
        if sid_size.value == 0:
            return False
        sid_buffer = ctypes.create_string_buffer(sid_size.value)
        domain_buffer = ctypes.create_unicode_buffer(max(1, domain_size.value))
        if not advapi32.LookupAccountNameW(
            None,
            candidate,
            ctypes.cast(sid_buffer, ctypes.c_void_p),
            ctypes.byref(sid_size),
            domain_buffer,
            ctypes.byref(domain_size),
            ctypes.byref(sid_use),
        ):
            return False
        return bool(advapi32.EqualSid(expected_ptr, ctypes.cast(sid_buffer, ctypes.c_void_p)))
    finally:
        kernel32.LocalFree(expected_ptr)


def validate_registered_task(task_xml: str, manifest: dict[str, Any]) -> tuple[bool, str]:
    """Validate the security/lifecycle semantics that make this task a ServerFS task."""
    try:
        root = ET.fromstring(task_xml)
    except ET.ParseError:
        return False, "registered task XML is invalid"

    sid = str(manifest.get("user_sid", ""))
    principal_user = _xml_text(root, ".//t:Principal/t:UserId")
    trigger_user = _xml_text(root, ".//t:LogonTrigger/t:UserId")
    # Task Scheduler canonicalizes default-valued XML by omitting these two
    # elements on Windows 11.  The Microsoft schema defines trigger Enabled's
    # default as true, and the scheduler's default run level is LUA/least
    # privilege.  An explicit non-default value still fails closed below.
    run_level = _xml_text(root, ".//t:Principal/t:RunLevel") or "LeastPrivilege"
    trigger_enabled = _xml_text(root, ".//t:LogonTrigger/t:Enabled") or "true"
    heartbeat_enabled = _xml_text(root, ".//t:TimeTrigger/t:Enabled") or "true"
    heartbeat_start = _xml_text(root, ".//t:TimeTrigger/t:StartBoundary")
    checks = {
        "principal user": _identity_matches_sid(principal_user, sid),
        "principal logon type": _xml_text(root, ".//t:Principal/t:LogonType") == "InteractiveToken",
        "principal run level": run_level == "LeastPrivilege",
        "logon trigger user": _identity_matches_sid(trigger_user, sid),
        "logon trigger enabled": trigger_enabled.lower() == "true",
        "logon trigger has no repetition": _xml_text(
            root, ".//t:LogonTrigger/t:Repetition/t:Interval"
        )
        == "",
        "heartbeat trigger enabled": heartbeat_enabled.lower() == "true",
        "heartbeat start boundary": bool(heartbeat_start),
        "heartbeat repetition interval": _xml_text(root, ".//t:TimeTrigger/t:Repetition/t:Interval")
        == SCHEDULER_REPETITION_INTERVAL,
        "heartbeat repetition indefinite": _xml_text(
            root, ".//t:TimeTrigger/t:Repetition/t:Duration"
        )
        == "",
        "heartbeat repetition keeps active instance": (
            _xml_text(root, ".//t:TimeTrigger/t:Repetition/t:StopAtDurationEnd") or "false"
        ).lower()
        == "false",
        "multiple instances": _xml_text(root, ".//t:Settings/t:MultipleInstancesPolicy")
        == "IgnoreNew",
        "execution limit": _xml_text(root, ".//t:Settings/t:ExecutionTimeLimit") == "PT0S",
        "restart interval": _xml_text(root, ".//t:RestartOnFailure/t:Interval")
        == SCHEDULER_RESTART_INTERVAL,
        "restart count": _xml_text(root, ".//t:RestartOnFailure/t:Count")
        == str(SCHEDULER_RESTART_COUNT),
        "task executable": os.path.normcase(_xml_text(root, ".//t:Actions/t:Exec/t:Command"))
        == os.path.normcase(str(manifest.get("task_executable", ""))),
        "task arguments": _xml_text(root, ".//t:Actions/t:Exec/t:Arguments")
        == subprocess.list2cmdline([str(value) for value in manifest.get("task_arguments", [])]),
        "working directory": os.path.normcase(
            _xml_text(root, ".//t:Actions/t:Exec/t:WorkingDirectory")
        )
        == os.path.normcase(str(Path(str(manifest.get("config_path", ""))).parent)),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        return False, f"registered task contract mismatch: {', '.join(failed)}"
    return True, ""


def _registered_enabled(task_xml: str) -> bool:
    try:
        root = ET.fromstring(task_xml)
    except ET.ParseError:
        return False
    return _xml_text(root, ".//t:Settings/t:Enabled").lower() != "false"


def _legacy_serverfs_executable() -> Path:
    return _local_app_data() / "ServerFS" / "connector-env" / "Scripts" / "serverfs.exe"


def _legacy_tunnel_client_executable() -> Path | None:
    from .tunnel_bootstrap import default_tunnel_client_path

    path = default_tunnel_client_path()
    return None if path is None else path.resolve()


def _identity_document(identity) -> dict[str, object]:
    return {
        "pid": identity.pid,
        "image_path": str(identity.image_path),
        "creation_time": identity.creation_time,
    }


def _identity_from_document(document: object):
    from .windows_process import ProcessIdentity

    if not isinstance(document, dict):
        return None
    pid = document.get("pid")
    image_path = document.get("image_path")
    creation_time = document.get("creation_time")
    if (
        not isinstance(pid, int)
        or pid <= 0
        or not isinstance(image_path, str)
        or not image_path
        or not isinstance(creation_time, int)
        or creation_time <= 0
    ):
        return None
    return ProcessIdentity(pid=pid, image_path=Path(image_path), creation_time=creation_time)


def _find_legacy_tunnel_roots() -> list[dict[str, object]]:
    """Find only the documented pre-v0.14 ``start-native.ps1`` process shape."""
    if sys.platform != "win32":
        return []

    from .windows_process import ProcessEntry, process_entries, process_identity

    expected_serverfs = _legacy_serverfs_executable().resolve()
    expected_tunnel = _legacy_tunnel_client_executable()
    if expected_tunnel is None:
        return []

    entries = process_entries()
    children: dict[int, list[ProcessEntry]] = {}
    for entry in entries:
        children.setdefault(entry.parent_pid, []).append(entry)

    roots: list[dict[str, object]] = []
    for entry in entries:
        if entry.executable_name.casefold() != expected_serverfs.name.casefold():
            continue
        root = process_identity(entry.pid)
        if root is None or os.path.normcase(str(root.image_path)) != os.path.normcase(
            str(expected_serverfs)
        ):
            continue

        # The real pre-v0.14 Windows chain is not a direct serverfs.exe ->
        # tunnel-client.exe edge: the uv/console-script launch path inserts
        # one or more Python processes.  Walk the whole descendant tree while
        # keeping the root itself exact-path pinned; a Task Scheduler managed
        # tree has no serverfs.exe root and therefore cannot match this shape.
        pending = list(children.get(entry.pid, ()))
        visited: set[int] = set()
        tunnel = None
        while pending:
            descendant = pending.pop()
            if descendant.pid in visited:
                continue
            visited.add(descendant.pid)
            if descendant.executable_name.casefold() == expected_tunnel.name.casefold():
                candidate = process_identity(descendant.pid)
                candidate_matches = candidate is not None and os.path.normcase(
                    str(candidate.image_path)
                ) == os.path.normcase(str(expected_tunnel))
                if candidate_matches:
                    tunnel = candidate
                    break
            pending.extend(children.get(descendant.pid, ()))

        if tunnel is not None:
            roots.append(
                {
                    "root": _identity_document(root),
                    "tunnel_client": _identity_document(tunnel),
                }
            )
    roots.sort(key=lambda item: int(item["root"]["pid"]))  # type: ignore[index]
    return roots


def _read_handover() -> dict[str, Any] | None:
    path = handover_path()
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WindowsServiceError("Windows lifecycle handover state is invalid") from exc
    if not isinstance(document, dict) or document.get("schema_version") != HANDOVER_SCHEMA_VERSION:
        raise WindowsServiceError("Windows lifecycle handover state is invalid")
    roots = document.get("roots")
    if not isinstance(roots, list):
        raise WindowsServiceError("Windows lifecycle handover state is invalid")
    return document


def _write_handover(roots: list[dict[str, object]]) -> None:
    document = {
        "schema_version": HANDOVER_SCHEMA_VERSION,
        "created_at": time.time(),
        "roots": roots,
        "initiator": None,
    }
    _atomic_write(
        handover_path(),
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def _set_handover_initiator() -> None:
    document = _read_handover()
    if document is None:
        return
    from .windows_process import process_identity

    identity = process_identity(os.getpid())
    if identity is None:
        raise WindowsServiceError("the handover initiator identity could not be established")
    updated = {**document, "initiator": _identity_document(identity)}
    _atomic_write(
        handover_path(),
        json.dumps(updated, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def _taskkill_executable() -> Path:
    executable = _system_directory() / "taskkill.exe"
    if not executable.is_file():
        raise WindowsServiceError("Windows taskkill utility is unavailable")
    return executable


def _terminate_legacy_root(identity) -> None:
    from .windows_process import same_process, wait_process_gone

    if not same_process(identity):
        return
    try:
        completed = subprocess.run(
            [str(_taskkill_executable()), "/PID", str(identity.pid), "/T", "/F"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WindowsServiceError(
            f"legacy Windows Tunnel could not be stopped ({type(exc).__name__})"
        ) from exc
    if completed.returncode != 0 and same_process(identity):
        raise WindowsServiceError("legacy Windows Tunnel could not be stopped")
    if not wait_process_gone(identity):
        raise WindowsServiceError("legacy Windows Tunnel did not stop")


def _perform_pending_handover() -> None:
    from .windows_process import same_process, wait_process_gone

    document = _read_handover()
    if document is None:
        if _find_legacy_tunnel_roots():
            raise WindowsServiceError(
                "an unmanaged legacy Windows Tunnel is running; reinstall the service "
                "to hand it over"
            )
        return

    initiator = _identity_from_document(document.get("initiator"))
    if initiator is not None and same_process(initiator):
        if not wait_process_gone(initiator, HANDOVER_INITIATOR_WAIT_SECONDS):
            raise WindowsServiceError("the lifecycle handover initiator did not exit")

    roots = document.get("roots", [])
    _append_lifecycle_event("legacy_handover_started", roots=len(roots))
    for entry in roots:
        if not isinstance(entry, dict):
            raise WindowsServiceError("Windows lifecycle handover state is invalid")
        root = _identity_from_document(entry.get("root"))
        if root is None:
            raise WindowsServiceError("Windows lifecycle handover state is invalid")
        _terminate_legacy_root(root)

    try:
        handover_path().unlink(missing_ok=True)
    except OSError as exc:
        raise WindowsServiceError(
            f"Windows lifecycle handover state could not be cleared ({type(exc).__name__})"
        ) from exc
    _append_lifecycle_event("legacy_handover_completed", roots=len(roots))


def install(config_path: Path, env_file: Path) -> dict[str, Any]:
    _ensure_windows()
    _validate_persistent_runtime_location()
    _validate_runtime_backend()
    config = config_path.resolve()
    env = env_file.resolve()
    if not config.is_file():
        raise WindowsServiceError("native serverfs.toml does not exist")
    values = _read_env_file(env)
    _tunnel_id(values)
    key_path = default_api_key_path()
    if not key_path.is_file():
        raise WindowsServiceError("Control Plane API key file is unavailable")

    user_sid = _current_user_sid()
    task_executable = _runner_executable()
    legacy_roots = _find_legacy_tunnel_roots()
    previous_registered_xml = _query_task_xml(optional=True)
    enabled = (
        _registered_enabled(previous_registered_xml)
        if previous_registered_xml is not None
        else False
    )
    task_xml = build_task_xml(
        user_sid=user_sid,
        task_executable=task_executable,
        config_path=config,
        env_file=env,
        enabled=enabled,
    )

    # Install/update is transactional across the OS registration and our two
    # persistent contract files.  Task Scheduler normalizes XML when it stores
    # a task, so post-registration verification is mandatory; if that check
    # fails, restore the exact pre-install state instead of leaving a half-
    # installed task/manifest pair behind.
    previous_manifest = manifest_path().read_bytes() if manifest_path().is_file() else None
    previous_task_file = task_xml_path().read_bytes() if task_xml_path().is_file() else None
    previous_handover = handover_path().read_bytes() if handover_path().is_file() else None
    rollback_path = service_home() / ".rollback-task.xml"

    try:
        _ensure_directory(service_log_dir())
        _atomic_write(task_xml_path(), task_xml)
        _write_manifest(
            config_path=config,
            env_file=env,
            user_sid=user_sid,
            task_executable=task_executable,
            task_xml=task_xml,
        )
        if legacy_roots:
            _write_handover(legacy_roots)
        else:
            handover_path().unlink(missing_ok=True)
        _run_schtasks("/Create", "/TN", TASK_NAME, "/XML", str(task_xml_path()), "/F")

        snapshot = status()
        if snapshot.get("task_valid") is not True:
            raise WindowsServiceError("registered Windows task failed contract verification")
        return snapshot
    except Exception as exc:
        rollback_failed = False
        try:
            if previous_registered_xml is None:
                _run_schtasks("/Delete", "/TN", TASK_NAME, "/F", check=False)
            else:
                _atomic_write(rollback_path, previous_registered_xml.encode("utf-16"))
                _run_schtasks("/Create", "/TN", TASK_NAME, "/XML", str(rollback_path), "/F")
        except Exception:
            rollback_failed = True
        finally:
            try:
                rollback_path.unlink(missing_ok=True)
            except OSError:
                rollback_failed = True

        for path, payload in (
            (manifest_path(), previous_manifest),
            (task_xml_path(), previous_task_file),
            (handover_path(), previous_handover),
        ):
            try:
                if payload is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, payload)
            except (OSError, WindowsServiceError):
                rollback_failed = True

        if rollback_failed:
            raise WindowsServiceError(
                "Windows service install failed and the previous state could not be fully restored"
            ) from exc
        raise


def start() -> dict[str, Any]:
    _ensure_windows()
    manifest = _manifest_document()
    task_xml = _query_task_xml()
    assert task_xml is not None
    valid, detail = validate_registered_task(task_xml, manifest)
    if not valid:
        raise WindowsServiceError(detail)
    _run_schtasks("/Change", "/TN", TASK_NAME, "/ENABLE")
    _set_handover_initiator()
    _run_schtasks("/Run", "/TN", TASK_NAME)
    return status()


def _read_json_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict) or document.get("schema_version") != STATE_SCHEMA_VERSION:
        return {}
    return document


def _read_state() -> dict[str, Any]:
    return _read_json_state(state_path())


def _read_watchdog_state() -> dict[str, Any]:
    return _read_json_state(watchdog_state_path())


def _process_image_path(pid: int) -> Path | None:
    if os.name != "nt" or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def _current_identity_state(prefix: str) -> dict[str, object]:
    from .windows_process import process_identity

    identity = process_identity(os.getpid())
    if identity is None:
        raise WindowsServiceError(f"Windows lifecycle {prefix} identity is unavailable")
    return {
        f"{prefix}_pid": identity.pid,
        f"{prefix}_image_path": str(identity.image_path),
        f"{prefix}_creation_time": identity.creation_time,
    }


def _runner_identity_state() -> dict[str, object]:
    """Capture the actual interpreter identity running the managed loop."""
    return _current_identity_state("runner")


def _watchdog_identity_state() -> dict[str, object]:
    """Capture the Task Scheduler-owned watchdog process identity."""
    return _current_identity_state("watchdog")


def _runner_alive(state: dict[str, Any], manifest: dict[str, Any] | None) -> bool:
    pid = state.get("runner_pid")
    if not isinstance(pid, int) or pid <= 0:
        return False

    image_path = state.get("runner_image_path")
    creation_time = state.get("runner_creation_time")
    if (
        isinstance(image_path, str)
        and image_path
        and isinstance(creation_time, int)
        and creation_time > 0
    ):
        from .windows_process import ProcessIdentity, same_process

        return same_process(
            ProcessIdentity(
                pid=pid,
                image_path=Path(image_path),
                creation_time=creation_time,
            )
        )

    # Pre-release development state without a creation-time identity is not
    # trusted: PID reuse or uv launcher indirection could otherwise report a
    # different process as the managed runner.
    del manifest
    return False


def _watchdog_alive(state: dict[str, Any]) -> bool:
    pid = state.get("watchdog_pid")
    image_path = state.get("watchdog_image_path")
    creation_time = state.get("watchdog_creation_time")
    if (
        not isinstance(pid, int)
        or pid <= 0
        or not isinstance(image_path, str)
        or not image_path
        or not isinstance(creation_time, int)
        or creation_time <= 0
    ):
        return False
    from .windows_process import ProcessIdentity, same_process

    return same_process(
        ProcessIdentity(pid=pid, image_path=Path(image_path), creation_time=creation_time)
    )


def _wait_runner_stopped(timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _runner_alive(_read_state(), None) and not _watchdog_alive(_read_watchdog_state()):
            return
        time.sleep(0.1)
    raise WindowsServiceError("Windows lifecycle watchdog/runner did not stop")


def _managed_identity(state: dict[str, Any], prefix: str):
    return _identity_from_document(
        {
            "pid": state.get(f"{prefix}_pid"),
            "image_path": state.get(f"{prefix}_image_path"),
            "creation_time": state.get(f"{prefix}_creation_time"),
        }
    )


def _terminate_managed_tree_if_alive() -> bool:
    """Kill only a frozen-identity managed tree when Task Scheduler cannot own cleanup.

    A scheduled task can be deleted or drifted while its action process keeps
    running.  In that state `/End` has no owner to target, so stop/uninstall
    must still be able to recover without killing by image name.  Prefer the
    watchdog root; if it is already gone, fall back to the recorded runner.
    PID + image path + creation time prevents PID-reuse mistakes.
    """
    from .windows_process import same_process, wait_process_gone

    candidates = (
        _managed_identity(_read_watchdog_state(), "watchdog"),
        _managed_identity(_read_state(), "runner"),
    )
    identity = next(
        (
            candidate
            for candidate in candidates
            if candidate is not None and same_process(candidate)
        ),
        None,
    )
    if identity is None:
        return False
    try:
        completed = subprocess.run(
            [str(_taskkill_executable()), "/PID", str(identity.pid), "/T", "/F"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WindowsServiceError(
            f"managed Windows lifecycle tree could not be stopped ({type(exc).__name__})"
        ) from exc
    if completed.returncode != 0 and same_process(identity):
        raise WindowsServiceError("managed Windows lifecycle tree could not be stopped")
    if not wait_process_gone(identity):
        raise WindowsServiceError("managed Windows lifecycle tree did not stop")
    return True


def stop() -> dict[str, Any]:
    _ensure_windows()
    task_registered = _query_task_xml(optional=True) is not None
    # Disable first whenever the scheduler still owns the task. An
    # operator-requested stop must never be interpreted as a failure that the
    # heartbeat or RestartOnFailure should relaunch.
    if task_registered:
        _run_schtasks("/Change", "/TN", TASK_NAME, "/DISABLE")
    state = _read_state()
    try:
        manifest = _manifest_document()
    except WindowsServiceError:
        manifest = None
    if _runner_alive(state, manifest):
        _write_state({**state, "stop_requested_at": time.time()})

    if task_registered:
        _run_schtasks("/End", "/TN", TASK_NAME, check=False)
        try:
            _wait_runner_stopped(SCHEDULER_END_GRACE_SECONDS)
        except WindowsServiceError:
            _terminate_managed_tree_if_alive()
            _wait_runner_stopped()
    else:
        # A task can disappear while its pythonw watchdog keeps running. The
        # recorded frozen identity is sufficient to recover that orphan safely.
        _terminate_managed_tree_if_alive()
        _wait_runner_stopped()
    return status()


def restart() -> dict[str, Any]:
    stop()
    return start()


def uninstall() -> None:
    _ensure_windows()
    task_registered = _query_task_xml(optional=True) is not None
    stop_error: WindowsServiceError | None = None
    try:
        # stop() also cleans a verified orphaned managed tree when the Task
        # Scheduler registration has already disappeared.
        stop()
    except WindowsServiceError as exc:
        stop_error = exc
    if task_registered:
        delete_result = _run_schtasks("/Delete", "/TN", TASK_NAME, "/F", check=False)
        if delete_result.returncode != 0 and _query_task_xml(optional=True) is not None:
            raise WindowsServiceError(
                "Windows persistent task could not be removed"
            ) from stop_error
        if stop_error is not None:
            raise WindowsServiceError(
                "Windows persistent task was removed after its running instance "
                "failed to stop cleanly"
            ) from stop_error
    elif stop_error is not None:
        raise WindowsServiceError(
            "orphaned Windows managed lifecycle failed to stop cleanly"
        ) from stop_error

    home = service_home()
    if home.exists():
        try:
            shutil.rmtree(home)
        except OSError as exc:
            detail = (
                "Windows persistent task was removed, but local service artifacts "
                "could not be removed"
                if task_registered
                else "Windows service artifacts could not be removed"
            )
            raise WindowsServiceError(f"{detail} ({type(exc).__name__})") from exc


def status() -> dict[str, Any]:
    _ensure_windows()
    task_xml: str | None = None
    task_query_error = ""
    registered: bool | None
    try:
        task_xml = _query_task_xml(optional=True)
    except WindowsServiceError as exc:
        registered = None
        task_query_error = str(exc)
    else:
        registered = task_xml is not None

    manifest: dict[str, Any] | None = None
    manifest_error = ""
    try:
        manifest = _manifest_document()
    except WindowsServiceError as exc:
        manifest_error = str(exc)

    task_valid = False
    task_error = task_query_error
    if task_xml is not None and manifest is not None:
        task_valid, task_error = validate_registered_task(task_xml, manifest)
    elif task_xml is not None:
        task_error = "registered task has no valid ServerFS manifest"

    handover_error = ""
    handover: dict[str, Any] | None = None
    handover_pending: bool | None
    try:
        handover = _read_handover()
    except WindowsServiceError as exc:
        handover_pending = None
        handover_error = str(exc)
    else:
        handover_pending = handover is not None

    state = _read_state()
    watchdog_state = _read_watchdog_state()
    watchdog_alive = _watchdog_alive(watchdog_state)
    runner_alive = _runner_alive(state, manifest)
    last_started = state.get("last_tunnel_started_at")
    last_exited = state.get("last_tunnel_exit_at")
    active_generation_was_open = bool(
        isinstance(last_started, (int, float))
        and (not isinstance(last_exited, (int, float)) or float(last_started) > float(last_exited))
    )
    runner_unclean_exit = bool(
        not runner_alive
        and isinstance(state.get("runner_pid"), int)
        and active_generation_was_open
        and state.get("stop_requested_at") is None
    )
    result: dict[str, Any] = {
        "backend": BACKEND,
        "task_name": TASK_NAME,
        "registered": registered,
        "enabled": (
            _registered_enabled(task_xml)
            if task_xml is not None
            else (False if registered is False else None)
        ),
        "task_valid": task_valid,
        "task_error": task_error,
        "task_query_error": task_query_error,
        "manifest_installed": manifest is not None,
        "manifest_error": manifest_error,
        "task_executable": (manifest.get("task_executable") if manifest is not None else None),
        "handover_pending": handover_pending,
        "handover_root_count": (len(handover.get("roots", [])) if handover is not None else 0),
        "handover_error": handover_error,
        "watchdog_pid": watchdog_state.get("watchdog_pid") if watchdog_alive else None,
        "watchdog_alive": watchdog_alive,
        "watchdog_started_at": watchdog_state.get("watchdog_started_at"),
        "runner_restart_count": watchdog_state.get("runner_restart_count"),
        "last_runner_started_at": watchdog_state.get("last_runner_started_at"),
        "last_runner_exit_at": watchdog_state.get("last_runner_exit_at"),
        "last_runner_exit_code": watchdog_state.get("last_runner_exit_code"),
        "next_runner_restart_delay_seconds": watchdog_state.get(
            "next_runner_restart_delay_seconds"
        ),
        "runner_pid": state.get("runner_pid") if runner_alive else None,
        "runner_alive": runner_alive,
        "runner_unclean_exit": runner_unclean_exit,
        "runner_started_at": state.get("runner_started_at"),
        "stop_requested_at": state.get("stop_requested_at"),
        "generation": state.get("generation"),
        "last_tunnel_started_at": state.get("last_tunnel_started_at"),
        "last_tunnel_exit_at": state.get("last_tunnel_exit_at"),
        "last_tunnel_exit_code": state.get("last_tunnel_exit_code"),
        "last_tunnel_runtime_seconds": state.get("last_tunnel_runtime_seconds"),
        "next_restart_delay_seconds": state.get("next_restart_delay_seconds"),
        "log_dir": str(service_log_dir()),
        "state_path": str(state_path()),
        "restart_policy": {
            "watchdog_runner_initial_seconds": WATCHDOG_INITIAL_RESTART_DELAY_SECONDS,
            "watchdog_runner_max_seconds": WATCHDOG_MAX_RESTART_DELAY_SECONDS,
            "watchdog_runner_healthy_reset_seconds": WATCHDOG_HEALTHY_RESET_SECONDS,
            "runner_tunnel_initial_seconds": INITIAL_RESTART_DELAY_SECONDS,
            "runner_tunnel_max_seconds": MAX_RESTART_DELAY_SECONDS,
            "runner_tunnel_healthy_reset_seconds": HEALTHY_RUN_RESET_SECONDS,
            "scheduler_repetition_interval": SCHEDULER_REPETITION_INTERVAL,
            "scheduler_interval": SCHEDULER_RESTART_INTERVAL,
            "scheduler_count": SCHEDULER_RESTART_COUNT,
        },
    }
    return result


def _write_json_state(path: Path, document: dict[str, Any]) -> None:
    payload = {"schema_version": STATE_SCHEMA_VERSION, **document}
    _atomic_write(path, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _write_state(document: dict[str, Any]) -> None:
    _write_json_state(state_path(), document)


def _write_watchdog_state(document: dict[str, Any]) -> None:
    _write_json_state(watchdog_state_path(), document)


def _append_lifecycle_event(event: str, **fields: object) -> None:
    _ensure_directory(service_log_dir())
    document = {
        "timestamp": time.time(),
        "event": event,
        **fields,
    }
    try:
        with lifecycle_log_path().open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
    except OSError as exc:
        raise WindowsServiceError(
            f"Windows lifecycle log could not be written ({type(exc).__name__})"
        ) from exc


def _apply_runtime_environment(values: dict[str, str]) -> None:
    # The service env file is authoritative for the narrow values the legacy
    # start-native.ps1 launcher injected before starting the supervisor.  Clear
    # absent names so ambient user/session variables cannot silently change the
    # managed service generation.
    for name in _RUNTIME_ENV_KEYS:
        value = values.get(name, "").strip()
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)


def _next_restart_delay(runtime_seconds: float, current_delay: float) -> float:
    if runtime_seconds >= HEALTHY_RUN_RESET_SECONDS:
        return INITIAL_RESTART_DELAY_SECONDS
    return min(
        MAX_RESTART_DELAY_SECONDS,
        max(INITIAL_RESTART_DELAY_SECONDS, current_delay * 2),
    )


def _next_watchdog_restart_delay(runtime_seconds: float, current_delay: float) -> float:
    if runtime_seconds >= WATCHDOG_HEALTHY_RESET_SECONDS:
        return WATCHDOG_INITIAL_RESTART_DELAY_SECONDS
    return min(
        WATCHDOG_MAX_RESTART_DELAY_SECONDS,
        max(WATCHDOG_INITIAL_RESTART_DELAY_SECONDS, current_delay * 2),
    )


def _background_creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _open_tunnel_logs() -> tuple[BinaryIO, BinaryIO]:
    _ensure_directory(service_log_dir())
    try:
        stdout = tunnel_stdout_path().open("ab", buffering=0)
        stderr = tunnel_stderr_path().open("ab", buffering=0)
    except OSError as exc:
        raise WindowsServiceError(
            f"Windows Tunnel logs could not be opened ({type(exc).__name__})"
        ) from exc
    return stdout, stderr


def run_watchdog(config_path: Path, env_file: Path) -> int:
    """Task Scheduler-owned host that keeps one managed runner alive."""
    _ensure_windows()
    config = config_path.resolve()
    env = env_file.resolve()
    if not config.is_file():
        raise WindowsServiceError("watchdog native serverfs.toml does not exist")
    values = _read_env_file(env)
    _tunnel_id(values)
    if not default_api_key_path().is_file():
        raise WindowsServiceError("Control Plane API key file is unavailable")

    watchdog_started_at = time.time()
    watchdog_identity = _watchdog_identity_state()
    restart_count = 0
    delay = WATCHDOG_INITIAL_RESTART_DELAY_SECONDS
    base_state: dict[str, Any] = {
        **watchdog_identity,
        "watchdog_started_at": watchdog_started_at,
        "runner_restart_count": restart_count,
        "managed_runner_pid": None,
        "last_runner_started_at": None,
        "last_runner_exit_at": None,
        "last_runner_exit_code": None,
        "last_runner_runtime_seconds": None,
        "next_runner_restart_delay_seconds": None,
    }
    _write_watchdog_state(base_state)
    _append_lifecycle_event("watchdog_started", watchdog_pid=watchdog_identity["watchdog_pid"])

    from .windows_job import WindowsJob

    while True:
        restart_count += 1
        started_wall = time.time()
        started_mono = time.monotonic()
        stderr = None
        job = WindowsJob()
        process: subprocess.Popen[bytes] | None = None
        exit_code = 2
        error_class = ""
        try:
            _ensure_directory(service_log_dir())
            stderr = runner_stderr_path().open("ab", buffering=0)
            job.open()
            process = subprocess.Popen(
                [str(_runner_executable()), *_runner_argv(config, env)],
                cwd=config.parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                creationflags=_background_creationflags(),
            )
            job.assign(process)
            current_state = {
                **base_state,
                "runner_restart_count": restart_count,
                "managed_runner_pid": process.pid,
                "last_runner_started_at": started_wall,
                "next_runner_restart_delay_seconds": None,
            }
            _write_watchdog_state(current_state)
            _append_lifecycle_event(
                "managed_runner_started",
                restart_count=restart_count,
                runner_pid=process.pid,
            )
            exit_code = process.wait()
        except Exception as exc:  # noqa: BLE001 - watchdog must keep recovering
            error_class = type(exc).__name__
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        finally:
            job.close()
            if stderr is not None:
                stderr.close()

        runtime = max(0.0, time.monotonic() - started_mono)
        restart_delay = delay
        exit_wall = time.time()
        base_state = {
            **watchdog_identity,
            "watchdog_started_at": watchdog_started_at,
            "runner_restart_count": restart_count,
            "managed_runner_pid": None,
            "last_runner_started_at": started_wall,
            "last_runner_exit_at": exit_wall,
            "last_runner_exit_code": exit_code,
            "last_runner_runtime_seconds": round(runtime, 3),
            "next_runner_restart_delay_seconds": restart_delay,
        }
        _write_watchdog_state(base_state)
        _append_lifecycle_event(
            "managed_runner_exited",
            restart_count=restart_count,
            exit_code=exit_code,
            runtime_seconds=round(runtime, 3),
            error_class=error_class,
            restart_delay_seconds=restart_delay,
        )
        time.sleep(restart_delay)
        delay = _next_watchdog_restart_delay(runtime, restart_delay)


def run_managed(config_path: Path, env_file: Path) -> int:
    """Run forever under the watchdog, supervising one Tunnel generation at a time."""
    _ensure_windows()
    config = config_path.resolve()
    env = env_file.resolve()
    if not config.is_file():
        raise WindowsServiceError("managed native serverfs.toml does not exist")
    values = _read_env_file(env)
    tunnel_id = _tunnel_id(values)
    api_key = default_api_key_path()
    if not api_key.is_file():
        raise WindowsServiceError("Control Plane API key file is unavailable")
    _apply_runtime_environment(values)

    runner_started_at = time.time()
    runner_identity = _runner_identity_state()
    generation = 0
    delay = INITIAL_RESTART_DELAY_SECONDS
    base_state: dict[str, Any] = {
        **runner_identity,
        "runner_started_at": runner_started_at,
        "generation": generation,
        "last_tunnel_started_at": None,
        "last_tunnel_exit_at": None,
        "last_tunnel_exit_code": None,
        "last_tunnel_runtime_seconds": None,
        "next_restart_delay_seconds": None,
    }
    _write_state(base_state)
    _append_lifecycle_event("runner_started", runner_pid=runner_identity["runner_pid"])

    _perform_pending_handover()

    try:
        with native_tunnel_instance_lock():
            from .windows_job import WindowsJob

            while True:
                generation += 1
                started_wall = time.time()
                started_mono = time.monotonic()
                current_state = {
                    **base_state,
                    "generation": generation,
                    "last_tunnel_started_at": started_wall,
                    "next_restart_delay_seconds": None,
                }
                _write_state(current_state)
                _append_lifecycle_event("tunnel_generation_started", generation=generation)

                stdout, stderr = _open_tunnel_logs()
                job = WindowsJob()
                exit_code = 2
                error_class = ""
                try:
                    job.open()
                    exit_code = run_native_tunnel(
                        config_path=config,
                        env_file=env,
                        tunnel_client=None,
                        tunnel_id=tunnel_id,
                        api_key_file=api_key,
                        job=job,
                        stdout=stdout,
                        stderr=stderr,
                        instance_lock_held=True,
                    )
                except Exception as exc:  # noqa: BLE001 - one generation may fail
                    error_class = type(exc).__name__
                    exit_code = 2
                finally:
                    job.close()
                    stdout.close()
                    stderr.close()

                runtime = max(0.0, time.monotonic() - started_mono)
                restart_delay = delay
                exit_wall = time.time()
                base_state = {
                    **runner_identity,
                    "runner_started_at": runner_started_at,
                    "generation": generation,
                    "last_tunnel_started_at": started_wall,
                    "last_tunnel_exit_at": exit_wall,
                    "last_tunnel_exit_code": exit_code,
                    "last_tunnel_runtime_seconds": round(runtime, 3),
                    "next_restart_delay_seconds": restart_delay,
                }
                _write_state(base_state)
                _append_lifecycle_event(
                    "tunnel_generation_exited",
                    generation=generation,
                    exit_code=exit_code,
                    runtime_seconds=round(runtime, 3),
                    error_class=error_class,
                    restart_delay_seconds=restart_delay,
                )
                time.sleep(restart_delay)
                delay = _next_restart_delay(runtime, restart_delay)
    except NativeTunnelError as exc:
        raise WindowsServiceError(str(exc)) from exc


__all__ = [
    "BACKEND",
    "HEALTHY_RUN_RESET_SECONDS",
    "INITIAL_RESTART_DELAY_SECONDS",
    "MAX_RESTART_DELAY_SECONDS",
    "SCHEDULER_RESTART_COUNT",
    "SCHEDULER_RESTART_INTERVAL",
    "TASK_NAME",
    "WindowsServiceError",
    "build_task_xml",
    "install",
    "restart",
    "run_managed",
    "run_watchdog",
    "start",
    "status",
    "stop",
    "uninstall",
    "validate_registered_task",
]

"""Independent second Windows Cloudflare service instance (Stage 2 source implementation).

This module owns a *second* persistent Cloudflare ingress for the same Windows
login, alongside the default ``ServerFS Native Tunnel`` Task Scheduler task. It is
strictly independent: own task name, own service root under
``%LOCALAPPDATA%\\ServerFS\\instances\\workpc-cloudflare``, own manifest/state/
task XML/logs, own named lifecycle mutex, own dedicated ``connector-env``
interpreter, and own watchdog -> runner -> kill-on-close Job Object generations.

What is deliberately *not* reused or mutated:

- the default ``windows_service`` Task Scheduler task, its manifest, state,
  task XML, logs or installed ``connector-env``;
- the SID-scoped OpenAI Tunnel instance mutex from ``native_tunnel``;
- the existing Agent Bridge owner and its shared writer-lease state.

Shared pieces are pure helpers only: the scheduled-task CLI runner, XML text
decoding, SID identity comparison, native runtime version check, generic process
identity helpers, ``WindowsJob`` containment and
``windows_cloudflare_runtime.run_cloudflare_generation``.

Parallel mode follows ordinary native ServerFS workdir, Agent and Repo Ops
policy, including writable roots. Both ingress processes can use the same TOML
configuration and the same current-user Agent Bridge and shared writer leases.
Retirement of the default service requires separately preserving ownership of
the shared Agent Bridge.

Stage status: source plus mocked tests only. No Task Scheduler operation, service
installation or credential inspection has been executed on a host; a live
Cloudflare deployment remains unverified.
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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from .native_config import NativeConfigError, load_native_config
from .windows_cloudflare_config import (
    WindowsCloudflareConfigurationError,
    validate_install,
)
from .windows_cloudflare_security import WindowsPrivateFileError, _no_reparse_components

#: Stable identifiers for the independent WorkPC Cloudflare instance. These are
#: product contract, not operator input: the task must never share a name with
#: the default ``ServerFS Native Tunnel`` task.
INSTANCE_ID = "workpc-cloudflare"
TASK_NAME = "ServerFS Cloudflare WorkPC"
TASK_URI = f"\\{TASK_NAME}"
BACKEND = "windows-task-scheduler-cloudflare-parallel"

#: Fixed public hostname and loopback origin for this instance, so the public
#: mapping cannot drift with an operator typo while the default service runs.
DEFAULT_HOSTNAME = "workpc-serverfs.ntlx.top"
HTTP_ORIGIN_PORT = 18765

MANIFEST_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
SCHEDULER_REPETITION_INTERVAL = "PT1M"
SCHEDULER_RESTART_INTERVAL = "PT1M"
SCHEDULER_RESTART_COUNT = 255
TASK_QUERY_ATTEMPTS = 3
TASK_QUERY_RETRY_SECONDS = 0.1
SCHEDULER_END_GRACE_SECONDS = 5.0
WATCHDOG_INITIAL_RESTART_DELAY_SECONDS = 2.0
WATCHDOG_MAX_RESTART_DELAY_SECONDS = 30.0
WATCHDOG_HEALTHY_RESET_SECONDS = 300.0
INITIAL_RESTART_DELAY_SECONDS = 5.0
MAX_RESTART_DELAY_SECONDS = 60.0
HEALTHY_RUN_RESET_SECONDS = 300.0
_TASK_NOT_FOUND_HRESULTS = frozenset({0x80070002, 0x80070003})

_SID_RE = re.compile(r"S-1-(?:[0-9]+-)+[0-9]+\Z")


class WindowsCloudflareParallelError(Exception):
    """Safe operator-facing parallel-instance failure, without secret material."""


@dataclass(frozen=True)
class ParallelDeployment:
    """Validated dedicated deployment for the parallel Cloudflare instance."""

    config_path: Path
    workdir_aliases: tuple[str, ...]
    workdir_roots: tuple[Path, ...]
    http_origin_port: int = HTTP_ORIGIN_PORT
    hostname: str = DEFAULT_HOSTNAME


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise WindowsCloudflareParallelError(
            "parallel Cloudflare service management requires Windows"
        )


def _local_app_data() -> Path:
    if os.name != "nt":  # test/import fallback only
        return Path.home() / "AppData" / "Local"
    from .native_lifecycle import window_local_app_data

    return window_local_app_data()


def instance_root() -> Path:
    """Private per-user state root; never the default service home."""
    return _local_app_data() / "ServerFS" / "instances" / INSTANCE_ID


def instance_connector_env() -> Path:
    """Dedicated current-user interpreter environment for this instance only."""
    return instance_root() / "connector-env"


def instance_manifest_path() -> Path:
    return instance_root() / "service.json"


def instance_task_xml_path() -> Path:
    return instance_root() / "task.xml"


def instance_state_path() -> Path:
    return instance_root() / "state.json"


def instance_watchdog_state_path() -> Path:
    return instance_root() / "watchdog.json"


def instance_log_dir() -> Path:
    return instance_root() / "logs"


def instance_lock_name(user_sid: str) -> str:
    """Namespaced current-user lifecycle lock for this instance only.

    The default OpenAI owner uses ``Local\\serverfs-native-tunnel-v1-<hash>``.
    A different prefix keeps the two generations independent by construction,
    instead of sharing one SID-scoped mutex and deadlocking each other.
    """
    if not _SID_RE.fullmatch(user_sid):
        raise WindowsCloudflareParallelError("current Windows user SID is invalid")
    digest = hashlib.sha256(f"serverfs-cloudflare-parallel-v1:{user_sid}".encode()).hexdigest()[:32]
    return f"Local\\serverfs-cloudflare-parallel-v1-{digest}"


def validate_distinct_ownership() -> None:
    """Refuse to run if this instance would share the default owner's identity."""
    from . import windows_service

    if TASK_NAME.casefold() == windows_service.TASK_NAME.casefold():
        raise WindowsCloudflareParallelError(
            "parallel Cloudflare task name collides with the default ServerFS task"
        )
    if os.path.normcase(str(instance_root())) == os.path.normcase(
        str(windows_service.service_home())
    ):
        raise WindowsCloudflareParallelError(
            "parallel Cloudflare instance root collides with the default service home"
        )


def validate_deployment_config(config_path: Path) -> ParallelDeployment:
    """Validate ordinary native TOML policy without mutating any workdir.

    The same config and roots as the OpenAI instance are allowed, including
    writable workdirs, Agent delegation and Repo Ops. The native parser owns
    those authorization rules; this function validates deployment paths.
    """
    candidate = Path(config_path).expanduser()
    if not candidate.is_absolute():
        raise WindowsCloudflareParallelError(
            "parallel Cloudflare config must have an absolute path"
        )
    try:
        _no_reparse_components(candidate)
        config = candidate.resolve(strict=True)
    except (WindowsPrivateFileError, OSError) as exc:
        raise WindowsCloudflareParallelError(
            "parallel Cloudflare config path cannot be validated"
        ) from exc
    if not config.is_file() or config.suffix.lower() != ".toml":
        raise WindowsCloudflareParallelError(
            "parallel Cloudflare config must be an existing .toml file"
        )

    try:
        workdirs, _settings = load_native_config(config)
    except NativeConfigError as exc:
        raise WindowsCloudflareParallelError(
            f"parallel Cloudflare config is invalid: {exc}"
        ) from exc

    aliases: list[str] = []
    roots: list[Path] = []
    for workdir in workdirs:
        root = Path(workdir.root)
        try:
            _no_reparse_components(root)
            resolved = root.resolve(strict=True)
        except (WindowsPrivateFileError, OSError) as exc:
            raise WindowsCloudflareParallelError(
                f"parallel Cloudflare workdir {workdir.alias!r} root cannot be validated"
            ) from exc
        if not resolved.is_dir():
            raise WindowsCloudflareParallelError(
                f"parallel Cloudflare workdir {workdir.alias!r} root is not a directory"
            )
        aliases.append(workdir.alias)
        roots.append(resolved)

    return ParallelDeployment(
        config_path=config,
        workdir_aliases=tuple(aliases),
        workdir_roots=tuple(roots),
    )


# -- current-user runtime --------------------------------------------------


def _current_user_sid() -> str:
    from .native_endpoint import current_user_sid

    try:
        return current_user_sid()
    except Exception as exc:  # noqa: BLE001 - normalize platform failure
        raise WindowsCloudflareParallelError(
            f"current Windows user SID is unavailable ({type(exc).__name__})"
        ) from exc


def _validate_instance_runtime_location() -> None:
    """Refuse to bind Task Scheduler to anything but this instance's connector-env."""
    active = Path(sys.prefix).resolve()
    expected = instance_connector_env().resolve()
    if os.path.normcase(str(active)) != os.path.normcase(str(expected)):
        raise WindowsCloudflareParallelError(
            "parallel Cloudflare install must run from its own connector-env "
            "(instances/workpc-cloudflare/connector-env)"
        )


def _validate_native_runtime() -> None:
    """Require the same serverfs/native version pairing as the default service."""
    from .windows_service import WindowsServiceError, _validate_runtime_backend

    try:
        _validate_runtime_backend()
    except WindowsServiceError as exc:
        raise WindowsCloudflareParallelError(str(exc)) from exc


def _runner_executable() -> Path:
    """The dedicated instance interpreter; never the default connector-env."""
    candidate = instance_connector_env() / "Scripts" / "pythonw.exe"
    if not candidate.is_file():
        raise WindowsCloudflareParallelError(
            "dedicated parallel instance pythonw.exe is unavailable in connector-env"
        )
    return candidate.resolve()


# -- scheduled task definition --------------------------------------------


def _qname(local_name: str) -> str:
    return f"{{{TASK_XML_NAMESPACE}}}{local_name}"


def _child(parent: ET.Element, name: str, text: str | None = None, **attributes: str) -> ET.Element:
    element = ET.SubElement(parent, _qname(name), attributes)
    if text is not None:
        element.text = text
    return element


def _task_start_boundary() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _schedule_argv(
    subcommand: str, config_path: Path, cloudflared: Path, token_file: Path, env_file: Path
) -> list[str]:
    return [
        "-m",
        "serverfs_mcp.cli",
        "service",
        "cloudflare-parallel",
        subcommand,
        "--config",
        str(config_path.resolve()),
        "--cloudflared",
        str(cloudflared.resolve()),
        "--token-file",
        str(token_file.resolve()),
        "--env-file",
        str(env_file.resolve()),
    ]


def _watchdog_argv(
    config_path: Path, cloudflared: Path, token_file: Path, env_file: Path
) -> list[str]:
    return _schedule_argv("_watchdog", config_path, cloudflared, token_file, env_file)


def _runner_argv(
    config_path: Path, cloudflared: Path, token_file: Path, env_file: Path
) -> list[str]:
    return _schedule_argv("_run", config_path, cloudflared, token_file, env_file)


def build_task_xml(
    *,
    user_sid: str,
    task_executable: Path,
    task_arguments: list[str],
    working_directory: Path,
    enabled: bool = True,
    start_boundary: str | None = None,
) -> bytes:
    """Render this instance's least-privilege Task Scheduler contract.

    Paths and fixed CLI values only: no credential value ever reaches task XML.
    """
    ET.register_namespace("", TASK_XML_NAMESPACE)
    task = ET.Element(_qname("Task"), {"version": "1.4"})

    registration = _child(task, "RegistrationInfo")
    _child(registration, "Author", user_sid)
    _child(
        registration,
        "Description",
        "ServerFS parallel Cloudflare ingress for the current Windows user",
    )
    _child(registration, "URI", TASK_URI)

    triggers = _child(task, "Triggers")
    logon = _child(triggers, "LogonTrigger")
    _child(logon, "Enabled", "true")
    _child(logon, "UserId", user_sid)

    heartbeat = _child(triggers, "TimeTrigger")
    heartbeat_repetition = _child(heartbeat, "Repetition")
    _child(heartbeat_repetition, "Interval", SCHEDULER_REPETITION_INTERVAL)
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
    _child(action, "Arguments", subprocess.list2cmdline(task_arguments))
    _child(action, "WorkingDirectory", str(working_directory.resolve()))

    xml = ET.tostring(task, encoding="unicode", xml_declaration=False)
    return ('<?xml version="1.0" encoding="UTF-16"?>\r\n' + xml).encode("utf-16")


# -- Windows Task Scheduler access ----------------------------------------


def _run_schtasks(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Run schtasks against this instance's task name only.

    The executable resolution is the one shared platform helper; every task-name
    argument is supplied by this module, so the default task cannot be targeted.
    """
    from .windows_service import _schtasks_executable

    try:
        completed = subprocess.run(
            [str(_schtasks_executable()), *args],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WindowsCloudflareParallelError(
            f"Windows Task Scheduler command could not run ({type(exc).__name__})"
        ) from exc
    if check and completed.returncode != 0:
        raise WindowsCloudflareParallelError(
            f"parallel Cloudflare Task Scheduler operation failed (exit={completed.returncode})"
        )
    return completed


def _query_task_xml(*, optional: bool = False) -> str | None:
    """Return only ``ServerFS Cloudflare WorkPC`` XML, distinguishing absence."""
    from .windows_service import _decode_process_output

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
            output = _decode_process_output(completed.stdout).lstrip("﻿\x00 \t\r\n")
            if not output:
                raise WindowsCloudflareParallelError(
                    "Windows Task Scheduler returned an empty task definition"
                )
            return output

        last_hresult = completed.returncode & 0xFFFFFFFF
        if attempt + 1 < TASK_QUERY_ATTEMPTS:
            time.sleep(TASK_QUERY_RETRY_SECONDS)

    if last_hresult in _TASK_NOT_FOUND_HRESULTS:
        if optional:
            return None
        raise WindowsCloudflareParallelError("parallel Cloudflare task is not registered")
    raise WindowsCloudflareParallelError(
        f"parallel Cloudflare Task Scheduler query failed (hresult=0x{last_hresult:08X})"
    )


def _xml_text(root: ET.Element, path: str) -> str:
    element = root.find(path, {"t": TASK_XML_NAMESPACE})
    return "" if element is None or element.text is None else element.text.strip()


def validate_registered_task(task_xml: str, manifest: dict[str, Any]) -> tuple[bool, str]:
    """Validate the security/lifecycle semantics of this instance's own task."""
    from .windows_service import _identity_matches_sid

    try:
        root = ET.fromstring(task_xml)
    except ET.ParseError:
        return False, "registered task XML is invalid"

    sid = str(manifest.get("user_sid", ""))
    principal_user = _xml_text(root, ".//t:Principal/t:UserId")
    trigger_user = _xml_text(root, ".//t:LogonTrigger/t:UserId")
    run_level = _xml_text(root, ".//t:Principal/t:RunLevel") or "LeastPrivilege"
    trigger_enabled = _xml_text(root, ".//t:LogonTrigger/t:Enabled") or "true"
    heartbeat_enabled = _xml_text(root, ".//t:TimeTrigger/t:Enabled") or "true"
    heartbeat_start = _xml_text(root, ".//t:TimeTrigger/t:StartBoundary")
    arguments = subprocess.list2cmdline(
        [str(value) for value in manifest.get("task_arguments", [])]
    )
    checks = {
        "task URI": _xml_text(root, ".//t:RegistrationInfo/t:URI") == TASK_URI,
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
        "task arguments": _xml_text(root, ".//t:Actions/t:Exec/t:Arguments") == arguments,
        "working directory": os.path.normcase(
            _xml_text(root, ".//t:Actions/t:Exec/t:WorkingDirectory")
        )
        == os.path.normcase(str(manifest.get("working_directory", ""))),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        return False, f"registered parallel task contract mismatch: {', '.join(failed)}"
    return True, ""


def _registered_enabled(task_xml: str) -> bool:
    try:
        root = ET.fromstring(task_xml)
    except ET.ParseError:
        return False
    return _xml_text(root, ".//t:Settings/t:Enabled").lower() != "false"


# -- instance-owned files --------------------------------------------------


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WindowsCloudflareParallelError(
            f"parallel Cloudflare directory could not be created ({type(exc).__name__})"
        ) from exc
    if not path.is_dir():
        raise WindowsCloudflareParallelError("parallel Cloudflare path is not a directory")


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
        raise WindowsCloudflareParallelError(
            f"parallel Cloudflare state could not be written ({type(exc).__name__})"
        ) from exc


def _manifest_document() -> dict[str, Any]:
    path = instance_manifest_path()
    if not path.is_file():
        raise WindowsCloudflareParallelError(
            "parallel Cloudflare instance manifest is not installed"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WindowsCloudflareParallelError(
            "parallel Cloudflare instance manifest is invalid"
        ) from exc
    if not isinstance(document, dict):
        raise WindowsCloudflareParallelError("parallel Cloudflare instance manifest is invalid")
    expected = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "backend": BACKEND,
        "instance_id": INSTANCE_ID,
        "task_name": TASK_NAME,
        "task_uri": TASK_URI,
        "provider": "cloudflare",
        "parallel_mode": True,
        "http_port": HTTP_ORIGIN_PORT,
        "hostname": DEFAULT_HOSTNAME,
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise WindowsCloudflareParallelError("parallel Cloudflare instance manifest is invalid")
    for key in ("config_path", "env_file", "token_file", "cloudflared", "task_executable"):
        value = document.get(key)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise WindowsCloudflareParallelError("parallel Cloudflare instance manifest is invalid")
    arguments = document.get("task_arguments")
    if (
        not isinstance(arguments, list)
        or not arguments
        or any(not isinstance(value, str) or not value for value in arguments)
    ):
        raise WindowsCloudflareParallelError("parallel Cloudflare instance manifest is invalid")
    working_directory = document.get("working_directory")
    if not isinstance(working_directory, str) or not Path(working_directory).is_absolute():
        raise WindowsCloudflareParallelError("parallel Cloudflare instance manifest is invalid")
    return document


def _write_manifest(
    *,
    config_path: Path,
    cloudflared: Path,
    token_file: Path,
    env_file: Path,
    user_sid: str,
    task_executable: Path,
    task_arguments: list[str],
    working_directory: Path,
    task_xml: bytes,
) -> None:
    """Persist paths and fixed policy only; credential values are never stored."""
    document = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "backend": BACKEND,
        "instance_id": INSTANCE_ID,
        "task_name": TASK_NAME,
        "task_uri": TASK_URI,
        "provider": "cloudflare",
        "parallel_mode": True,
        "http_port": HTTP_ORIGIN_PORT,
        "hostname": DEFAULT_HOSTNAME,
        "user_sid": user_sid,
        "config_path": str(config_path.resolve()),
        "env_file": str(env_file.resolve()),
        "token_file": str(token_file.resolve()),
        "cloudflared": str(cloudflared.resolve()),
        "task_executable": str(task_executable.resolve()),
        "task_arguments": list(task_arguments),
        "working_directory": str(working_directory.resolve()),
        "task_xml_sha256": hashlib.sha256(task_xml).hexdigest(),
    }
    _atomic_write(
        instance_manifest_path(),
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


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
    return _read_json_state(instance_state_path())


def _read_watchdog_state() -> dict[str, Any]:
    return _read_json_state(instance_watchdog_state_path())


def _write_json_state(path: Path, document: dict[str, Any]) -> None:
    payload = {"schema_version": STATE_SCHEMA_VERSION, **document}
    _atomic_write(path, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _write_state(document: dict[str, Any]) -> None:
    _write_json_state(instance_state_path(), document)


def _write_watchdog_state(document: dict[str, Any]) -> None:
    _write_json_state(instance_watchdog_state_path(), document)


def _append_lifecycle_event(event: str, **fields: object) -> None:
    _ensure_directory(instance_log_dir())
    document = {"timestamp": time.time(), "event": event, **fields}
    try:
        with (instance_log_dir() / "lifecycle.log").open(
            "a", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
    except OSError as exc:
        raise WindowsCloudflareParallelError(
            f"parallel Cloudflare lifecycle log could not be written ({type(exc).__name__})"
        ) from exc


# -- process identity and containment -------------------------------------


def _identity_document(identity: Any) -> dict[str, object]:
    return {
        "pid": identity.pid,
        "image_path": str(identity.image_path),
        "creation_time": identity.creation_time,
    }


def _identity_from_document(document: object) -> Any | None:
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


def _managed_identity(state: dict[str, Any], prefix: str) -> Any | None:
    return _identity_from_document(
        {
            "pid": state.get(f"{prefix}_pid"),
            "image_path": state.get(f"{prefix}_image_path"),
            "creation_time": state.get(f"{prefix}_creation_time"),
        }
    )


def _current_identity_state(prefix: str) -> dict[str, object]:
    from .windows_process import process_identity

    identity = process_identity(os.getpid())
    if identity is None:
        raise WindowsCloudflareParallelError(
            f"parallel Cloudflare {prefix} identity is unavailable"
        )
    return {
        f"{prefix}_pid": identity.pid,
        f"{prefix}_image_path": str(identity.image_path),
        f"{prefix}_creation_time": identity.creation_time,
    }


def _managed_alive(state: dict[str, Any], prefix: str) -> bool:
    identity = _managed_identity(state, prefix)
    if identity is None:
        return False
    from .windows_process import same_process

    return same_process(identity)


def _runner_alive(state: dict[str, Any]) -> bool:
    return _managed_alive(state, "runner")


def _watchdog_alive(state: dict[str, Any]) -> bool:
    return _managed_alive(state, "watchdog")


def _kill_process_tree(identity: Any) -> None:
    """Kill one frozen identity and its descendants; never a process by name."""
    from .windows_process import same_process, wait_process_gone
    from .windows_service import _taskkill_executable

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
        raise WindowsCloudflareParallelError(
            f"parallel Cloudflare process tree could not be stopped ({type(exc).__name__})"
        ) from exc
    if completed.returncode != 0 and same_process(identity):
        raise WindowsCloudflareParallelError(
            "parallel Cloudflare process tree could not be stopped"
        )
    if not wait_process_gone(identity):
        raise WindowsCloudflareParallelError("parallel Cloudflare process tree did not stop")


def _terminate_managed_tree_if_alive() -> bool:
    """Recover an orphaned instance tree from its own frozen identities only."""
    from .windows_process import same_process

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
    _kill_process_tree(identity)
    return True


def _wait_stopped(timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _runner_alive(_read_state()) and not _watchdog_alive(_read_watchdog_state()):
            return
        time.sleep(0.1)
    raise WindowsCloudflareParallelError("parallel Cloudflare watchdog/runner did not stop")


@contextmanager
def _instance_lock():
    """Hold this instance's own named mutex for one runner lifetime."""
    if os.name != "nt":
        yield
        return

    import ctypes

    name = instance_lock_name(_current_user_sid())
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise WindowsCloudflareParallelError(
            "the parallel Cloudflare ownership mutex is unavailable"
        )
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        raise WindowsCloudflareParallelError(
            "another parallel Cloudflare instance is already running"
        )
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


# -- provider revalidation -------------------------------------------------


def _log_path(name: str) -> Path:
    return instance_log_dir() / name


def _open_log(name: str) -> BinaryIO:
    _ensure_directory(instance_log_dir())
    try:
        return _log_path(name).open("ab", buffering=0)
    except OSError as exc:
        raise WindowsCloudflareParallelError(
            f"parallel Cloudflare logs could not be opened ({type(exc).__name__})"
        ) from exc


def _spec_from_manifest(
    manifest: dict[str, Any],
    config_path: Path,
    cloudflared: Path,
    token_file: Path,
    env_file: Path,
) -> Any:
    """Revalidate pinned binary and private credentials before every generation."""
    for key, expected in (
        ("config_path", config_path),
        ("cloudflared", cloudflared),
        ("token_file", token_file),
        ("env_file", env_file),
    ):
        recorded = Path(str(manifest.get(key, "")))
        if not recorded.is_absolute() or os.path.normcase(
            str(recorded.resolve())
        ) != os.path.normcase(str(Path(expected).resolve())):
            raise WindowsCloudflareParallelError("parallel Cloudflare manifest path mismatch")
    try:
        spec = validate_install(
            config_path,
            cloudflared,
            token_file,
            env_file,
            HTTP_ORIGIN_PORT,
            DEFAULT_HOSTNAME,
            user_sid=_current_user_sid(),
        )
    except (KeyError, TypeError, WindowsCloudflareConfigurationError) as exc:
        raise WindowsCloudflareParallelError(
            "parallel Cloudflare provider configuration is invalid"
        ) from exc
    if (
        spec.config_path != Path(config_path).resolve()
        or Path(spec.env_file) != Path(env_file).resolve()
    ):
        raise WindowsCloudflareParallelError("parallel Cloudflare provider path mismatch")
    return spec


# -- lifecycle -------------------------------------------------------------


def _preflight(config_path: Path, cloudflared: Path, token_file: Path, env_file: Path) -> Any:
    """Read-only validation shared by install and every managed startup."""
    _ensure_windows()
    validate_distinct_ownership()
    deployment = validate_deployment_config(config_path)
    _validate_native_runtime()
    spec = validate_install(
        deployment.config_path,
        cloudflared,
        token_file,
        env_file,
        HTTP_ORIGIN_PORT,
        DEFAULT_HOSTNAME,
        user_sid=_current_user_sid(),
    )
    if spec.config_path != deployment.config_path:
        raise WindowsCloudflareParallelError("parallel Cloudflare config path mismatch")
    return spec


def install_instance(
    config_path: Path, cloudflared: Path, token_file: Path, env_file: Path
) -> dict[str, Any]:
    """Validate, then transactionally register this instance's disabled task.

    The default ServerFS task, manifest, state, logs and connector-env are never
    read, created, changed or removed. Install never starts the instance.
    """
    _ensure_windows()
    _validate_instance_runtime_location()
    spec = _preflight(config_path, cloudflared, token_file, env_file)
    user_sid = _current_user_sid()
    task_executable = _runner_executable()
    working_directory = instance_root()
    task_arguments = _watchdog_argv(
        spec.config_path, spec.cloudflared, spec.token_file, spec.env_file
    )

    previous_registered_xml = _query_task_xml(optional=True)
    if previous_registered_xml is not None:
        previous_status = status_instance()
        if not previous_status.get("task_valid"):
            raise WindowsCloudflareParallelError(
                "existing parallel Cloudflare task contract is invalid; "
                "uninstall or repair it before reinstalling"
            )
        if previous_status.get("watchdog_alive") or previous_status.get("runner_alive"):
            raise WindowsCloudflareParallelError(
                "stop the existing parallel Cloudflare instance before reinstalling"
            )
    enabled = (
        _registered_enabled(previous_registered_xml)
        if previous_registered_xml is not None
        else False
    )
    task_xml = build_task_xml(
        user_sid=user_sid,
        task_executable=task_executable,
        task_arguments=task_arguments,
        working_directory=working_directory,
        enabled=enabled,
    )

    previous_manifest = (
        instance_manifest_path().read_bytes() if instance_manifest_path().is_file() else None
    )
    previous_task_file = (
        instance_task_xml_path().read_bytes() if instance_task_xml_path().is_file() else None
    )
    rollback_path = instance_root() / ".rollback-task.xml"

    try:
        _ensure_directory(instance_log_dir())
        _atomic_write(instance_task_xml_path(), task_xml)
        _write_manifest(
            config_path=spec.config_path,
            cloudflared=spec.cloudflared,
            token_file=spec.token_file,
            env_file=spec.env_file,
            user_sid=user_sid,
            task_executable=task_executable,
            task_arguments=task_arguments,
            working_directory=working_directory,
            task_xml=task_xml,
        )
        _run_schtasks("/Create", "/TN", TASK_NAME, "/XML", str(instance_task_xml_path()), "/F")
        snapshot = status_instance()
        if snapshot.get("task_valid") is not True:
            raise WindowsCloudflareParallelError(
                "registered parallel Cloudflare task failed contract verification"
            )
        _append_lifecycle_event("parallel_instance_installed", enabled=enabled)
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
            (instance_manifest_path(), previous_manifest),
            (instance_task_xml_path(), previous_task_file),
        ):
            try:
                if payload is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, payload)
            except (OSError, WindowsCloudflareParallelError):
                rollback_failed = True

        if rollback_failed:
            raise WindowsCloudflareParallelError(
                "parallel Cloudflare install failed and the previous state "
                "could not be fully restored"
            ) from exc
        raise


def _require_valid_task() -> dict[str, Any]:
    manifest = _manifest_document()
    task_xml = _query_task_xml()
    assert task_xml is not None
    valid, detail = validate_registered_task(task_xml, manifest)
    if not valid:
        raise WindowsCloudflareParallelError(detail)
    return manifest


def start_instance() -> dict[str, Any]:
    """Enable and run only this instance's task; refuse a drifted contract."""
    _ensure_windows()
    _require_valid_task()
    _run_schtasks("/Change", "/TN", TASK_NAME, "/ENABLE")
    _run_schtasks("/Run", "/TN", TASK_NAME)
    return status_instance()


def stop_instance() -> dict[str, Any]:
    """Disable this instance's task first, then end and verify its own tree."""
    _ensure_windows()
    task_registered = _query_task_xml(optional=True) is not None
    if task_registered:
        _run_schtasks("/Change", "/TN", TASK_NAME, "/DISABLE")
    state = _read_state()
    if _runner_alive(state) or _watchdog_alive(_read_watchdog_state()):
        _write_state({**state, "stop_requested_at": time.time()})

    if task_registered:
        _run_schtasks("/End", "/TN", TASK_NAME, check=False)
        try:
            _wait_stopped(SCHEDULER_END_GRACE_SECONDS)
        except WindowsCloudflareParallelError:
            _terminate_managed_tree_if_alive()
            _wait_stopped()
    else:
        _terminate_managed_tree_if_alive()
        _wait_stopped()
    return status_instance()


def restart_instance() -> dict[str, Any]:
    stop_instance()
    return start_instance()


def uninstall_instance() -> None:
    """Stop and unregister this instance, then remove its own artifacts."""
    _ensure_windows()
    task_registered = _query_task_xml(optional=True) is not None
    stop_error: WindowsCloudflareParallelError | None = None
    try:
        stop_instance()
    except WindowsCloudflareParallelError as exc:
        stop_error = exc
    if task_registered:
        delete_result = _run_schtasks("/Delete", "/TN", TASK_NAME, "/F", check=False)
        if delete_result.returncode != 0 and _query_task_xml(optional=True) is not None:
            raise WindowsCloudflareParallelError(
                "parallel Cloudflare task could not be removed"
            ) from stop_error
        if stop_error is not None:
            raise WindowsCloudflareParallelError(
                "parallel Cloudflare task was removed after its running instance "
                "failed to stop cleanly"
            ) from stop_error
    elif stop_error is not None:
        raise WindowsCloudflareParallelError(
            "orphaned parallel Cloudflare lifecycle failed to stop cleanly"
        ) from stop_error

    home = instance_root()
    if home.exists():
        try:
            shutil.rmtree(home)
        except OSError as exc:
            detail = (
                "parallel Cloudflare task was removed, but local instance artifacts "
                "could not be removed"
                if task_registered
                else "parallel Cloudflare instance artifacts could not be removed"
            )
            raise WindowsCloudflareParallelError(f"{detail} ({type(exc).__name__})") from exc


def status_instance() -> dict[str, Any]:
    """Truthful read-only status for this instance; never inspects the default task."""
    _ensure_windows()
    task_xml: str | None = None
    task_query_error = ""
    registered: bool | None
    try:
        task_xml = _query_task_xml(optional=True)
    except WindowsCloudflareParallelError as exc:
        registered = None
        task_query_error = str(exc)
    else:
        registered = task_xml is not None

    manifest: dict[str, Any] | None = None
    manifest_error = ""
    try:
        manifest = _manifest_document()
    except WindowsCloudflareParallelError as exc:
        manifest_error = str(exc)

    task_valid = False
    task_error = task_query_error
    if task_xml is not None and manifest is not None:
        task_valid, task_error = validate_registered_task(task_xml, manifest)
    elif task_xml is not None:
        task_error = "registered task has no valid parallel instance manifest"

    state = _read_state()
    watchdog_state = _read_watchdog_state()
    watchdog_alive = _watchdog_alive(watchdog_state)
    runner_alive = _runner_alive(state)
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
    return {
        "backend": BACKEND,
        "instance_id": INSTANCE_ID,
        "provider": "cloudflare",
        "parallel_mode": True,
        "agent_bridge_owner": "shared-existing",
        "repo_ops_policy": "native-config",
        "task_name": TASK_NAME,
        "task_uri": TASK_URI,
        "hostname": DEFAULT_HOSTNAME,
        "http_origin_port": HTTP_ORIGIN_PORT,
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
        "http_origin_alive": _managed_alive(state, "origin"),
        "cloudflare_connector_alive": _managed_alive(state, "connector"),
        "instance_root": str(instance_root()),
        "log_dir": str(instance_log_dir()),
        "state_path": str(instance_state_path()),
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


# -- managed watchdog / runner --------------------------------------------


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


def run_watchdog(config_path: Path, cloudflared: Path, token_file: Path, env_file: Path) -> int:
    """Task Scheduler-owned host keeping one managed runner in a Job Object alive."""
    _ensure_windows()
    manifest = _manifest_document()
    config = Path(config_path).resolve()
    _spec_from_manifest(
        manifest,
        config,
        Path(cloudflared),
        Path(token_file),
        Path(env_file),
    )
    validate_deployment_config(config)

    watchdog_started_at = time.time()
    watchdog_identity = _current_identity_state("watchdog")
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
    _append_lifecycle_event(
        "parallel_watchdog_started", watchdog_pid=watchdog_identity["watchdog_pid"]
    )

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
            stderr = _open_log("runner.stderr.log")
            job.open()
            process = subprocess.Popen(
                [
                    str(_runner_executable()),
                    *_runner_argv(
                        Path(config_path),
                        Path(cloudflared),
                        Path(token_file),
                        Path(env_file),
                    ),
                ],
                cwd=instance_root(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                creationflags=_background_creationflags(),
            )
            job.assign(process)
            _write_watchdog_state(
                {
                    **base_state,
                    "runner_restart_count": restart_count,
                    "managed_runner_pid": process.pid,
                    "last_runner_started_at": started_wall,
                    "next_runner_restart_delay_seconds": None,
                }
            )
            _append_lifecycle_event(
                "parallel_runner_started",
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
            "parallel_runner_exited",
            restart_count=restart_count,
            exit_code=exit_code,
            runtime_seconds=round(runtime, 3),
            error_class=error_class,
            restart_delay_seconds=restart_delay,
        )
        time.sleep(restart_delay)
        delay = _next_watchdog_restart_delay(runtime, restart_delay)


def run_managed(config_path: Path, cloudflared: Path, token_file: Path, env_file: Path) -> int:
    """Run forever under the watchdog, supervising one Cloudflare generation."""
    _ensure_windows()
    manifest = _manifest_document()
    config = Path(config_path).resolve()
    deployment = validate_deployment_config(config)
    spec = _spec_from_manifest(
        manifest,
        config,
        Path(cloudflared),
        Path(token_file),
        Path(env_file),
    )
    if deployment.config_path != spec.config_path:
        raise WindowsCloudflareParallelError("parallel Cloudflare config path mismatch")

    runner_started_at = time.time()
    runner_identity = _current_identity_state("runner")
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
    _append_lifecycle_event("parallel_runner_started", runner_pid=runner_identity["runner_pid"])

    from .windows_cloudflare_runtime import run_cloudflare_generation
    from .windows_job import WindowsJob
    from .windows_process import process_identity

    with _instance_lock():
        while True:
            generation += 1
            started_wall = time.time()
            started_mono = time.monotonic()
            current_state: dict[str, Any] = {
                **base_state,
                "generation": generation,
                "last_tunnel_started_at": started_wall,
                "next_restart_delay_seconds": None,
            }
            for role in ("origin", "connector"):
                current_state.update(
                    {
                        f"{role}_pid": None,
                        f"{role}_image_path": None,
                        f"{role}_creation_time": None,
                    }
                )
            _write_state(current_state)
            _append_lifecycle_event("parallel_tunnel_generation_started", generation=generation)

            connector_stdout = _open_log("cloudflare.stdout.log")
            connector_stderr = _open_log("cloudflare.stderr.log")
            origin_stdout = _open_log("http-origin.stdout.log")
            origin_stderr = _open_log("http-origin.stderr.log")
            job = WindowsJob()
            exit_code = 2
            error_class = ""
            try:
                job.open()

                def on_child_started(
                    role: str, process: Any, *, state: dict[str, Any] = current_state
                ) -> None:
                    identity = process_identity(process.pid)
                    if identity is None:
                        raise WindowsCloudflareParallelError(
                            "parallel Cloudflare child identity is unavailable"
                        )
                    state.update(
                        {
                            f"{role}_pid": identity.pid,
                            f"{role}_image_path": str(identity.image_path),
                            f"{role}_creation_time": identity.creation_time,
                        }
                    )
                    _write_state(state)
                    _append_lifecycle_event(
                        "parallel_cloudflare_child_started", role=role, pid=identity.pid
                    )

                spec = _spec_from_manifest(
                    manifest,
                    config,
                    Path(cloudflared),
                    Path(token_file),
                    Path(env_file),
                )
                exit_code = run_cloudflare_generation(
                    on_child_started=on_child_started,
                    python_executable=Path(sys.executable),
                    config_path=config,
                    cloudflared_executable=spec.cloudflared,
                    token_file=spec.token_file,
                    http_port=spec.port,
                    origin_environment=spec.origin_environment,
                    job=job,
                    origin_stdout=origin_stdout,
                    origin_stderr=origin_stderr,
                    connector_stdout=connector_stdout,
                    connector_stderr=connector_stderr,
                )
            except Exception as exc:  # noqa: BLE001 - one generation may fail
                error_class = type(exc).__name__
                exit_code = 2
            finally:
                job.close()
                connector_stdout.close()
                connector_stderr.close()
                origin_stdout.close()
                origin_stderr.close()

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
                "parallel_tunnel_generation_exited",
                generation=generation,
                exit_code=exit_code,
                runtime_seconds=round(runtime, 3),
                error_class=error_class,
                restart_delay_seconds=restart_delay,
            )
            time.sleep(restart_delay)
            delay = _next_restart_delay(runtime, restart_delay)


def describe_instance() -> dict[str, Any]:
    """Read-only identity metadata; safe to call before any lifecycle exists."""
    return {
        "instance_id": INSTANCE_ID,
        "task_name": TASK_NAME,
        "task_uri": TASK_URI,
        "instance_root": str(instance_root()),
        "connector_env": str(instance_connector_env()),
        "http_origin_port": HTTP_ORIGIN_PORT,
        "hostname": DEFAULT_HOSTNAME,
        "parallel_mode": True,
        "agent_bridge_owner": "shared-existing",
        "repo_ops_policy": "native-config",
        "owns_default_task": False,
    }


__all__ = [
    "BACKEND",
    "DEFAULT_HOSTNAME",
    "HTTP_ORIGIN_PORT",
    "INSTANCE_ID",
    "TASK_NAME",
    "TASK_URI",
    "ParallelDeployment",
    "WindowsCloudflareParallelError",
    "build_task_xml",
    "describe_instance",
    "install_instance",
    "instance_connector_env",
    "instance_lock_name",
    "instance_log_dir",
    "instance_manifest_path",
    "instance_root",
    "instance_state_path",
    "instance_task_xml_path",
    "instance_watchdog_state_path",
    "restart_instance",
    "run_managed",
    "run_watchdog",
    "start_instance",
    "status_instance",
    "stop_instance",
    "uninstall_instance",
    "validate_deployment_config",
    "validate_distinct_ownership",
    "validate_registered_task",
]

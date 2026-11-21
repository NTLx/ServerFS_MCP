"""Scoped Windows current-user Core lifecycle control (v0.25.0 W2).

One independent Task Scheduler Core owner with authenticated input preflight,
verified PID/creation-time and loopback listener identity. No operation touches
a Bridge, legacy Tunnel or ingress connector Task Scheduler registration.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import windows_core_service as runtime
from . import windows_core_task_spec as task_contract

_TASK_NOT_FOUND = frozenset({0x80070002, 0x80070003})
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_STATE_SCHEMA = 1


class CoreControlError(Exception):
    """Redacted component-only lifecycle refusal."""


def service_home() -> Path:
    from .native_lifecycle import window_local_app_data

    return window_local_app_data() / "ServerFS" / "service-manager" / "windows" / "mcp-core"


def manifest_path() -> Path:
    return service_home() / "service.json"


def task_xml_path() -> Path:
    return service_home() / "task.xml"


def state_path() -> Path:
    return service_home() / "state.json"


@contextmanager
def core_owner_mutex():
    """Single current-user Core owner across scheduled and accidental direct runs.

    The handle remains open for the entire HTTP server lifetime. The mutex
    does not own Bridge, Cloudflare or OpenAI connector process trees.
    """
    runtime._ensure_windows()
    import ctypes

    sid = _current_sid()
    name = (
        "Local\\serverfs-mcp-core-v1-"
        + hashlib.sha256(sid.upper().encode("ascii")).hexdigest()[:32]
    )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise CoreControlError("Core current-user owner lock is unavailable")
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        raise CoreControlError("another Core owner already holds the user lock")
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


def _current_sid() -> str:
    from .native_endpoint import current_user_sid

    try:
        return current_user_sid()
    except Exception as exc:  # noqa: BLE001 - no raw Windows account details
        raise CoreControlError("Core current-user SID is unavailable") from exc


def _check_private(path: Path, *, directory: bool, sid: str) -> None:
    """Require an existing, current-user-owned, protected-DACL regular object."""
    from .windows_cloudflare_security import (
        _no_reparse_components,
        _read_windows_file_sddl,
        _validate_private_sddl,
    )

    try:
        _no_reparse_components(path)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) & _REPARSE):
            raise CoreControlError("Core service path is redirected")
        if directory and not stat.S_ISDIR(info.st_mode):
            raise CoreControlError("Core private service home is not a directory")
        if not directory and not stat.S_ISREG(info.st_mode):
            raise CoreControlError("Core service artifact is not a regular file")
        _validate_private_sddl(_read_windows_file_sddl(path), sid)
    except CoreControlError:
        raise
    except Exception as exc:  # noqa: BLE001 - never disclose DACL/private paths
        raise CoreControlError("Core service private Windows DACL validation failed") from exc


def _private_home(*, sid: str) -> Path:
    runtime._ensure_windows()
    home = service_home()
    from .native_lifecycle import window_local_app_data

    expected = window_local_app_data() / "ServerFS" / "service-manager" / "windows" / "mcp-core"
    if os.path.normcase(str(home)) != os.path.normcase(str(expected)):
        raise CoreControlError("Core service home is outside its fixed current-user location")
    # Unlike older installer code, do not create a permissively inherited
    # directory and then use it. A prepared protected DACL is a hard gate.
    _check_private(home, directory=True, sid=sid)
    return home


def _dedicated_pythonw() -> Path:
    from .native_lifecycle import window_local_app_data

    prefix = window_local_app_data() / "ServerFS" / "core-owner-env"
    if os.path.normcase(str(Path(sys.prefix).resolve())) != os.path.normcase(str(prefix.resolve())):
        raise CoreControlError("Core requires its dedicated current-user Python environment")
    exe = prefix / "Scripts" / "pythonw.exe"
    if not exe.is_file():
        raise CoreControlError("Core dedicated Python interpreter is unavailable")
    _check_private(exe, directory=False, sid=_current_sid())
    return exe


def _require_separate_root(config: Path, home: Path) -> None:
    from .windows_cloudflare_security import _within

    _config, workdirs = runtime._verified_config(config)
    private = home.resolve(strict=True)
    if any(_within(private, root) or _within(root, private) for root in workdirs):
        raise CoreControlError("Core service storage overlaps an exposed workdir")


def _task_call(*args: str) -> subprocess.CompletedProcess[bytes]:
    from .windows_service import _schtasks_executable

    try:
        return subprocess.run(
            [str(_schtasks_executable()), *args],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CoreControlError("Core Task Scheduler operation failed") from exc


def _registered_xml(*, optional: bool = False) -> str | None:
    from .windows_service import _decode_process_output

    result = _task_call("/Query", "/TN", task_contract.TASK_NAME, "/XML", "/HResult")
    if result.returncode == 0:
        xml = _decode_process_output(result.stdout).lstrip("\ufeff\x00 \t\r\n")
        if not xml:
            raise CoreControlError("Core Task Scheduler returned an empty definition")
        return xml
    if result.returncode & 0xFFFFFFFF in _TASK_NOT_FOUND and optional:
        return None
    raise CoreControlError("Core Task Scheduler task is absent or query failed")


def _expect_task_success(*args: str) -> None:
    if _task_call(*args).returncode != 0:
        raise CoreControlError("Core Task Scheduler mutation failed")


def _task_enabled(xml: str) -> bool:
    from xml.etree import ElementTree as ET

    from .windows_component_task import NAMESPACE

    try:
        node = ET.fromstring(xml).find("./t:Settings/t:Enabled", {"t": NAMESPACE})
    except ET.ParseError as exc:
        raise CoreControlError("Core Task Scheduler XML is invalid") from exc
    if node is None or node.text not in ("true", "false"):
        raise CoreControlError("Core Task Scheduler enabled state is ambiguous")
    return node.text == "true"


def _atomic_create(path: Path, payload: bytes) -> None:
    """Exclusive same-volume artifact publish, never overwrite an existing file."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        from .windows_private_artifacts import protect_new_file

        protect_new_file(tmp, _current_sid())
        _check_private(tmp, directory=False, sid=_current_sid())
        os.link(tmp, path)
    except OSError as exc:
        raise CoreControlError("Core service artifact staging failed") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _read_manifest() -> dict[str, Any]:
    sid = _current_sid()
    _private_home(sid=sid)
    path = manifest_path()
    _check_private(path, directory=False, sid=sid)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CoreControlError("Core service manifest is unreadable") from exc
    valid, _ = task_contract.validate_manifest(data)
    if not valid or data["sid"] != sid:
        raise CoreControlError("Core service manifest is invalid or belongs to another user")
    return data


def _read_state() -> dict[str, Any]:
    sid = _current_sid()
    try:
        _check_private(state_path(), directory=False, sid=sid)
        state = json.loads(state_path().read_text(encoding="utf-8"))
    except (CoreControlError, OSError, ValueError):
        return {}
    if not isinstance(state, dict) or state.get("schema_version") != _STATE_SCHEMA:
        return {}
    return state


def _state_is_pristine() -> bool:
    state = _read_state()
    return (
        state.get("component") == "mcp-core"
        and state.get("stage") == "disabled-never-started"
        and state.get("core_pid") is None
        and state.get("core_image_path") is None
        and state.get("core_creation_time") is None
    )


def _require_owned() -> tuple[dict[str, Any], str]:
    manifest = _read_manifest()
    authored = task_xml_path()
    _check_private(authored, directory=False, sid=manifest["sid"])
    valid, _ = task_contract.validate_authored_xml(authored.read_bytes(), manifest)
    if not valid:
        raise CoreControlError("Core source task XML failed identity validation")
    registered = _registered_xml()
    if registered is None:
        raise CoreControlError("Core task is not registered")
    valid, _ = task_contract.validate_registered_xml(registered, manifest)
    if not valid:
        raise CoreControlError("Core registered task identity has drifted")
    return manifest, registered


def _write_state(value: dict[str, Any], *, replace_existing: bool) -> None:
    """Never expose raw authorization headers in service state."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = state_path()
    if not replace_existing:
        _atomic_create(path, payload)
        return
    sid = _current_sid()
    _check_private(path, directory=False, sid=sid)
    tmp = path.with_name(f".{path.name}.run-{os.getpid()}")
    try:
        with tmp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        from .windows_private_artifacts import protect_new_file

        protect_new_file(tmp, sid)
        _check_private(tmp, directory=False, sid=sid)
        os.replace(tmp, path)
    except OSError as exc:
        raise CoreControlError("Core process state could not be recorded") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def install_disabled(spec: task_contract.CoreTaskSpec) -> dict[str, Any]:
    """Fresh disabled install; refuse existing/ambiguous registration.

    The operator must pre-provision a private current-user service directory
    and dedicated signed wheel/interpreter. No ACL repair or implicit start.
    """
    runtime._ensure_windows()
    spec = spec.validated()
    if spec.enabled:
        raise CoreControlError("Core staging requires a disabled task definition")
    sid = _current_sid()
    if spec.sid != sid or spec.pythonw != _dedicated_pythonw():
        raise CoreControlError("Core task interpreter or current-user SID differs")
    home = _private_home(sid=sid)
    _require_separate_root(spec.config_path, home)
    runtime.validate_runtime(
        runtime.CoreRuntimeSpec(spec.config_path, spec.auth_file, spec.port, spec.hostname, sid),
        require_free_port=True,
    )
    if _registered_xml(optional=True) is not None:
        raise CoreControlError("Core task name is already registered")
    files = (manifest_path(), task_xml_path(), state_path())
    if any(x.exists() or x.is_symlink() for x in files):
        raise CoreControlError("Core service artifacts already exist")
    xml = task_contract.build_task_xml(spec)
    manifest = task_contract.manifest_for(spec, xml)
    valid, _ = task_contract.validate_authored_xml(xml, manifest)
    if not valid:
        raise CoreControlError("Core task XML authoring failed")
    state = {
        "schema_version": _STATE_SCHEMA,
        "component": "mcp-core",
        "stage": "disabled-never-started",
        "core_pid": None,
        "core_image_path": None,
        "core_creation_time": None,
    }
    created = False
    try:
        _atomic_create(task_xml_path(), xml)
        _atomic_create(
            manifest_path(),
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        _write_state(state, replace_existing=False)
        for file in files:
            _check_private(file, directory=False, sid=sid)
        _expect_task_success(
            "/Create", "/TN", task_contract.TASK_NAME, "/XML", str(task_xml_path())
        )
        created = True
        registered = _registered_xml()
        if registered is None:
            raise CoreControlError("Core registered task disappeared")
        valid, _ = task_contract.validate_registered_xml(registered, manifest)
        if not valid or _task_enabled(registered):
            raise CoreControlError("Core installed task is not verified disabled")
        snapshot = status()
        if not (
            snapshot["registered"] is True
            and snapshot["task_valid"] is True
            and snapshot["enabled"] is False
        ):
            raise CoreControlError("Core registered task status is inconsistent")
        return snapshot
    except Exception as original:  # noqa: BLE001 - conditional rollback only
        deletable = False
        try:
            registered = _registered_xml(optional=True)
            if registered is None:
                deletable = True
            elif created:
                valid, _ = task_contract.validate_registered_xml(registered, manifest)
                if valid and not _task_enabled(registered) and _state_is_pristine():
                    _expect_task_success("/Delete", "/TN", task_contract.TASK_NAME, "/F")
                    deletable = _registered_xml(optional=True) is None
        except Exception:  # noqa: BLE001 - unknown OS state: preserve evidence
            pass
        if not deletable:
            raise CoreControlError("Core installation failed; rollback incomplete") from original
        for file in files:
            if file.exists():
                try:
                    _check_private(file, directory=False, sid=sid)
                    file.unlink()
                except Exception as exc:  # noqa: BLE001 - cannot claim full cleanup
                    raise CoreControlError("Core installation rollback incomplete") from exc
        raise


def _require_previous_generation_stopped() -> None:
    """Fail closed on unknown state or a still-live previous Core process.

    A gone PID and a re-used PID with different creation time are distinct
    from an inaccessible still-running PID. Never infer death solely from
    a failed OpenProcess call.
    """
    if _state_is_pristine():
        return
    previous = _read_state()
    pid = previous.get("core_pid")
    image = previous.get("core_image_path")
    created = previous.get("core_creation_time")
    if (
        previous.get("component") != "mcp-core"
        or previous.get("stage") != "owner-started"
        or type(pid) is not int
        or pid <= 0
        or not isinstance(image, str)
        or not image
        or type(created) is not int
        or created <= 0
    ):
        raise CoreControlError("Core previous generation history is unverified")
    from .windows_process import WindowsProcessError, process_entries, process_identity

    try:
        current = process_identity(pid)
        if current is not None:
            if current.creation_time == created and os.path.normcase(
                str(current.image_path)
            ) == os.path.normcase(image):
                raise CoreControlError("Core previous generation is still alive")
            # A reused PID with a distinct kernel creation time is not the old Core.
            return
        if any(entry.pid == pid for entry in process_entries()):
            raise CoreControlError("Core previous process identity cannot be read")
    except WindowsProcessError as exc:
        raise CoreControlError("Core previous process termination is unverified") from exc


def record_runtime_owner(spec: runtime.CoreRuntimeSpec) -> None:
    """Require exact task and current process identity before HTTP startup.

    This records a generation marker *before* opening a socket; a previous
    owner cannot be mistaken for a never-started installation afterward.
    """
    manifest, registered = _require_owned()
    if not _task_enabled(registered):
        raise CoreControlError("Core Task Scheduler entry is disabled")
    if (
        manifest["config_path"] != str(spec.config_path.resolve())
        or manifest["auth_file"] != str(spec.auth_file.resolve())
        or manifest["port"] != spec.port
        or manifest["hostname"] != spec.hostname
        or manifest["sid"] != spec.user_sid
        or os.path.normcase(manifest["pythonw"]) != os.path.normcase(sys.executable)
    ):
        raise CoreControlError("Core runtime identity disagrees with registered task")
    _require_previous_generation_stopped()
    from .windows_process import process_identity, registered_pythonw_image_matches

    identity = process_identity(os.getpid())
    if identity is None or not registered_pythonw_image_matches(
        registered_launcher=Path(manifest["pythonw"]),
        kernel_image=identity.image_path,
        venv_prefix=Path(sys.prefix),
        current_executable=Path(sys.executable),
        base_executable=(
            Path(sys._base_executable)
            if isinstance(getattr(sys, "_base_executable", None), str)
            else None
        ),
        require_pythonw_entrypoint=True,
    ):
        raise CoreControlError("Core process image cannot be proven")
    _write_state(
        {
            "schema_version": _STATE_SCHEMA,
            "component": "mcp-core",
            "stage": "owner-started",
            "core_pid": identity.pid,
            "core_image_path": str(identity.image_path),
            "core_creation_time": identity.creation_time,
        },
        replace_existing=True,
    )


def status() -> dict[str, Any]:
    """Read-only: process identity is not listener ownership or MCP readiness."""
    runtime._ensure_windows()
    try:
        manifest, registered = _require_owned()
        valid, error = True, ""
        enabled: bool | None = _task_enabled(registered)
    except CoreControlError as exc:
        error, valid, enabled, manifest = str(exc), False, None, None
        try:
            registered = _registered_xml(optional=True)
        except CoreControlError:
            registered = None
    state = _read_state()
    from .windows_process import ProcessIdentity, same_process

    pid = state.get("core_pid")
    created = state.get("core_creation_time")
    image = state.get("core_image_path")
    alive = (
        type(pid) is int
        and pid > 0
        and type(created) is int
        and created > 0
        and isinstance(image, str)
        and bool(image)
        and same_process(ProcessIdentity(pid, Path(image), created))
    )
    listener_owner = "not_verified"
    if alive and valid and manifest is not None:
        from .windows_core_listener import prove_core_listener

        try:
            prove_core_listener(
                expected_port=manifest["port"],
                expected_hostname=manifest["hostname"],
            )
            listener_owner = "verified"
        except Exception:  # noqa: BLE001 - OS inspection failures are not readiness
            listener_owner = "not_verified"
    return {
        "component": "mcp-core",
        "task_name": task_contract.TASK_NAME,
        "registered": registered is not None,
        "task_valid": valid,
        "enabled": enabled,
        "manifest_installed": manifest is not None,
        "task_error": error,
        "core_alive": bool(alive),
        "core_pid": pid if alive else None,
        "http_readiness": "not_verified",
        "listener_owner": listener_owner,
    }


def pause() -> dict[str, Any]:
    """Disable future starts only. Running Core sessions are not terminated."""
    _require_owned()
    _expect_task_success("/Change", "/TN", task_contract.TASK_NAME, "/DISABLE")
    updated = _registered_xml()
    if updated is None or _task_enabled(updated):
        raise CoreControlError("Core task did not become disabled")
    return status()


def stop() -> dict[str, Any]:
    """Intentionally stop only the owned Core, leaving Bridge/ingress untouched."""
    _, xml = _require_owned()
    if _task_enabled(xml):
        _expect_task_success("/Change", "/TN", task_contract.TASK_NAME, "/DISABLE")
        _, xml = _require_owned()
        if _task_enabled(xml):
            raise CoreControlError("Core Task Scheduler did not remain disabled")

    snapshot = status()
    if snapshot["core_alive"]:
        # Refuse to kill a PID directly. /End applies solely to our verified
        # registered task and Task Scheduler owns the process being stopped.
        result = _task_call("/End", "/TN", task_contract.TASK_NAME)
        if result.returncode != 0 and status()["core_alive"]:
            raise CoreControlError("Core scheduled task could not be stopped")
        return _wait_for_core(alive=False)

    if not _state_is_pristine():
        _require_previous_generation_stopped()
    return status()


def _wait_for_core(*, alive: bool, timeout_seconds: float = 20.0) -> dict[str, Any]:
    """Wait for measured Core identity/listener, never infer readiness from /Run."""
    import time

    deadline = time.monotonic() + timeout_seconds
    while True:
        snapshot = status()
        if not snapshot["task_valid"]:
            raise CoreControlError("Core registered owner drifted during lifecycle change")
        if alive:
            if snapshot["core_alive"] and snapshot["listener_owner"] == "verified":
                return snapshot
        elif not snapshot["core_alive"]:
            _require_previous_generation_stopped()
            return snapshot
        if time.monotonic() >= deadline:
            raise CoreControlError("Core lifecycle proof timed out")
        time.sleep(0.2)


def start() -> dict[str, Any]:
    """Activate this registered Core alone, verifying exact TCP listener ownership."""
    manifest, registered = _require_owned()
    snapshot = status()
    if snapshot["core_alive"]:
        if snapshot["listener_owner"] != "verified":
            raise CoreControlError("Core process is active but listener owner is unverified")
        if not _task_enabled(registered):
            _expect_task_success("/Change", "/TN", task_contract.TASK_NAME, "/ENABLE")
        return _wait_for_core(alive=True)

    _require_previous_generation_stopped()
    # Preflight credentials, policy and port BEFORE enabling the scheduled task.
    runtime.validate_runtime(
        runtime.CoreRuntimeSpec(
            Path(manifest["config_path"]),
            Path(manifest["auth_file"]),
            manifest["port"],
            manifest["hostname"],
            manifest["sid"],
        ),
        require_free_port=True,
    )
    enabled_here = not _task_enabled(registered)
    if enabled_here:
        _expect_task_success("/Change", "/TN", task_contract.TASK_NAME, "/ENABLE")
    try:
        _, enabled_xml = _require_owned()
        if not _task_enabled(enabled_xml):
            raise CoreControlError("Core registered task did not become enabled")
        _expect_task_success("/Run", "/TN", task_contract.TASK_NAME)
        return _wait_for_core(alive=True)
    except Exception:
        # The exact registered contract must still be owned before rollback.
        # Disabling future triggers is safer than repeatedly relaunching an
        # unverified generation every minute. Never /End an unknown PID.
        if enabled_here:
            try:
                _, current = _require_owned()
                if _task_enabled(current):
                    _expect_task_success("/Change", "/TN", task_contract.TASK_NAME, "/DISABLE")
            except Exception as rollback_error:
                raise CoreControlError(
                    "Core activation failed; rollback incomplete"
                ) from rollback_error
        raise


def restart() -> dict[str, Any]:
    """Restart only Core; the Bridge and connector tasks are never touched."""
    stop()
    return start()


def uninstall_offline() -> None:
    """Only a never-started, disabled task; no live session is terminated."""
    _, xml = _require_owned()
    if _task_enabled(xml) or not _state_is_pristine():
        raise CoreControlError("Core offline removal is unsafe")
    _expect_task_success("/Delete", "/TN", task_contract.TASK_NAME, "/F")
    if _registered_xml(optional=True) is not None:
        raise CoreControlError("Core registered task remains after removal")
    for file in (task_xml_path(), manifest_path(), state_path()):
        _check_private(file, directory=False, sid=_current_sid())
        file.unlink()

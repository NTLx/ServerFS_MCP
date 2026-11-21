"""Scoped Windows connector lifecycle control (v0.25.0 W2).

Each connector has its own exact Task Scheduler registration, private state,
PID and restart policy. No operation mutates Core, Bridge or legacy ingress.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import windows_connector_service as runtime
from . import windows_connector_task_spec as tasks

_NOT_FOUND = frozenset({0x80070002, 0x80070003})


class ConnectorControlError(Exception):
    """Fixed, credential-free connector lifecycle refusal."""


def _require_kind(kind: str) -> str:
    if kind not in tasks.TASK_NAMES:
        raise ConnectorControlError("connector kind is unsupported")
    return kind


def _sid() -> str:
    from .windows_core_control import _current_sid

    return _current_sid()


def _check_home(kind: str) -> None:
    runtime._check_private(runtime.service_home(kind), directory=True)
    logs = runtime.service_home(kind) / "logs"
    runtime._check_private(logs, directory=True)
    # Never implicitly create a log file with inherited/ambient permissions.
    for name in ("connector.stdout.log", "connector.stderr.log"):
        runtime._check_private(logs / name, directory=False)


def _pythonw(kind: str) -> Path:
    from .native_lifecycle import window_local_app_data

    prefix = window_local_app_data() / "ServerFS" / f"{kind}-connector-env"
    if os.path.normcase(str(Path(sys.prefix).resolve())) != os.path.normcase(str(prefix.resolve())):
        raise ConnectorControlError("connector requires its dedicated Python environment")
    pythonw = prefix / "Scripts" / "pythonw.exe"
    runtime._check_private(pythonw, directory=False)
    return pythonw


def _call(*args: str) -> subprocess.CompletedProcess[bytes]:
    from .windows_service import _schtasks_executable

    try:
        return subprocess.run(
            [str(_schtasks_executable()), *args],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConnectorControlError("connector Task Scheduler operation failed") from exc


def _registered(kind: str, *, optional: bool = False) -> str | None:
    from .windows_service import _decode_process_output

    _require_kind(kind)
    result = _call("/Query", "/TN", tasks.TASK_NAMES[kind], "/XML", "/HResult")
    if result.returncode == 0:
        xml = _decode_process_output(result.stdout).lstrip("\ufeff\x00 \t\r\n")
        if not xml:
            raise ConnectorControlError("connector registered task XML is empty")
        return xml
    if result.returncode & 0xFFFFFFFF in _NOT_FOUND and optional:
        return None
    raise ConnectorControlError("connector Task Scheduler identity is unavailable")


def _expect(*args: str) -> None:
    if _call(*args).returncode != 0:
        raise ConnectorControlError("connector Task Scheduler mutation failed")


def _enabled(xml: str) -> bool:
    from xml.etree import ElementTree as ET

    from .windows_component_task import NAMESPACE

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ConnectorControlError("connector task XML is invalid") from exc
    value = root.findtext("./t:Settings/t:Enabled", namespaces={"t": NAMESPACE})
    if value not in ("true", "false"):
        raise ConnectorControlError("connector task enabled state is ambiguous")
    return value == "true"


def _manifest(kind: str) -> dict[str, Any]:
    _check_home(kind)
    runtime._check_private(runtime.manifest_path(kind), directory=False)
    try:
        data = json.loads(runtime.manifest_path(kind).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectorControlError("connector service manifest is unavailable") from exc
    valid, _ = tasks.validate_manifest(data)
    if not valid or data["kind"] != kind or data["sid"] != _sid():
        raise ConnectorControlError("connector task manifest is foreign or invalid")
    if os.path.normcase(data["policy_file"]) != os.path.normcase(str(runtime.policy_path(kind))):
        raise ConnectorControlError("connector task policy is outside its fixed private home")
    runtime._check_private(runtime.policy_path(kind), directory=False)
    policy_bytes = runtime.policy_path(kind).read_bytes()
    if hashlib.sha256(policy_bytes).hexdigest() != data["policy_sha256"]:
        raise ConnectorControlError("connector policy differs from installed task")
    return data


def _read_state(kind: str) -> dict[str, Any]:
    try:
        runtime._check_private(runtime.state_path(kind), directory=False)
        value = json.loads(runtime.state_path(kind).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unknown state is not a safe offline claim
        return {}
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return {}
    return value


def _pristine(kind: str) -> bool:
    state = _read_state(kind)
    return (
        state.get("component") == kind + "-connector"
        and state.get("stage") == "disabled-never-started"
        and state.get("connector_pid") is None
        and state.get("connector_image_path") is None
        and state.get("connector_creation_time") is None
    )


def _write_state(kind: str, state: dict[str, Any], *, replace: bool) -> None:
    from .windows_core_control import _atomic_create

    path = runtime.state_path(kind)
    raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not replace:
        _atomic_create(path, raw)
        return
    runtime._check_private(path, directory=False)
    tmp = path.with_name(f".{path.name}.run-{os.getpid()}")
    try:
        with tmp.open("xb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        from .windows_private_artifacts import protect_new_file

        protect_new_file(tmp, _sid())
        runtime._check_private(tmp, directory=False)
        os.replace(tmp, path)
    except OSError as exc:
        raise ConnectorControlError("connector state update failed") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _owned(kind: str) -> tuple[dict[str, Any], str]:
    manifest = _manifest(kind)
    runtime._check_private(runtime.task_path(kind), directory=False)
    valid, _ = tasks.validate_authored_task(runtime.task_path(kind).read_bytes(), manifest)
    if not valid:
        raise ConnectorControlError("connector authored task XML is not valid")
    xml = _registered(kind)
    if xml is None:
        raise ConnectorControlError("connector task is not registered")
    valid, _ = tasks.validate_registered_task(xml, manifest)
    if not valid:
        raise ConnectorControlError("connector registered task has drifted")
    return manifest, xml


def install_disabled(kind: str) -> dict[str, Any]:
    """Requires pre-provisioned private policy, credentials, logs, interpreter."""
    runtime._check_windows()
    _require_kind(kind)
    _check_home(kind)
    policy = runtime.load_policy(kind)
    runtime.validate_runtime_inputs(kind, policy)
    interpreter = _pythonw(kind)
    if _registered(kind, optional=True) is not None:
        raise ConnectorControlError("connector task name already exists")
    files = (
        runtime.manifest_path(kind),
        runtime.task_path(kind),
        runtime.state_path(kind),
    )
    if any(file.exists() or file.is_symlink() for file in files):
        raise ConnectorControlError("connector staging artifacts already exist")
    spec = tasks.ConnectorTaskSpec(
        kind=kind,
        sid=_sid(),
        pythonw=interpreter,
        policy_file=runtime.policy_path(kind),
        enabled=False,
    ).validated()
    policy_bytes = runtime.policy_path(kind).read_bytes()
    xml = tasks.build_task_xml(spec)
    manifest = tasks.build_manifest(spec, xml, policy_bytes)
    state = {
        "schema_version": 1,
        "component": kind + "-connector",
        "stage": "disabled-never-started",
        "connector_pid": None,
        "connector_image_path": None,
        "connector_creation_time": None,
    }
    created = False
    try:
        from .windows_core_control import _atomic_create

        _atomic_create(runtime.task_path(kind), xml)
        _atomic_create(
            runtime.manifest_path(kind),
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        _write_state(kind, state, replace=False)
        for file in files:
            runtime._check_private(file, directory=False)
        _expect("/Create", "/TN", tasks.TASK_NAMES[kind], "/XML", str(runtime.task_path(kind)))
        created = True
        registered = _registered(kind)
        valid, _ = tasks.validate_registered_task(registered or "", manifest)
        if not valid or registered is None or _enabled(registered):
            raise ConnectorControlError("connector installed task is not disabled or valid")
        snapshot = status(kind)
        if not (
            snapshot["registered"] is True
            and snapshot["task_valid"] is True
            and snapshot["enabled"] is False
        ):
            raise ConnectorControlError("connector installed task status disagrees")
        return snapshot
    except Exception as original:  # noqa: BLE001 - conditional, evidence-preserving rollback
        clean = False
        try:
            current = _registered(kind, optional=True)
            if current is None:
                clean = True
            elif created:
                valid, _ = tasks.validate_registered_task(current, manifest)
                if valid and not _enabled(current) and _pristine(kind):
                    _expect("/Delete", "/TN", tasks.TASK_NAMES[kind], "/F")
                    clean = _registered(kind, optional=True) is None
        except Exception:  # noqa: BLE001 - unknown state cannot justify deletion
            pass
        if not clean:
            raise ConnectorControlError(
                "connector installation failed; rollback incomplete"
            ) from original
        for file in files:
            if file.exists():
                runtime._check_private(file, directory=False)
                file.unlink()
        raise


def _previous_gone(kind: str) -> None:
    if _pristine(kind):
        return
    old = _read_state(kind)
    pid = old.get("connector_pid")
    image = old.get("connector_image_path")
    created = old.get("connector_creation_time")
    if (
        old.get("component") != kind + "-connector"
        or old.get("stage") != "owner-started"
        or type(pid) is not int
        or pid <= 0
        or not isinstance(image, str)
        or not image
        or type(created) is not int
        or created <= 0
    ):
        raise ConnectorControlError("connector previous owner history is unverified")
    from .windows_process import process_entries, process_identity

    original = process_identity(pid)
    if original is not None:
        if original.creation_time == created and os.path.normcase(str(original.image_path)) == (
            os.path.normcase(image)
        ):
            raise ConnectorControlError("previous connector generation remains alive")
        return
    if any(entry.pid == pid for entry in process_entries()):
        raise ConnectorControlError("previous connector process identity is inaccessible")


def record_runtime_owner(kind: str) -> None:
    """This task's process identity is stamped before launching its connector."""
    manifest, xml = _owned(kind)
    if not _enabled(xml):
        raise ConnectorControlError("connector scheduled task is disabled")
    if os.path.normcase(str(sys.executable)) != os.path.normcase(manifest["pythonw"]):
        raise ConnectorControlError("connector interpreter is not the registered executable")
    _previous_gone(kind)
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
        raise ConnectorControlError("connector current process identity is unverified")
    _write_state(
        kind,
        {
            "schema_version": 1,
            "component": kind + "-connector",
            "stage": "owner-started",
            "connector_pid": identity.pid,
            "connector_image_path": str(identity.image_path),
            "connector_creation_time": identity.creation_time,
        },
        replace=True,
    )


def status(kind: str) -> dict[str, Any]:
    """Return only this Connector's Task state and exact process identity."""
    _require_kind(kind)
    runtime._check_windows()
    manifest: dict[str, Any] | None
    try:
        manifest, xml = _owned(kind)
        valid, reason, enabled = True, "", _enabled(xml)
    except ConnectorControlError as exc:
        manifest, valid, reason, enabled = None, False, str(exc), None
        try:
            xml = _registered(kind, optional=True)
        except ConnectorControlError:
            xml = None
    state = _read_state(kind)
    pid, created, image = (
        state.get("connector_pid"),
        state.get("connector_creation_time"),
        state.get("connector_image_path"),
    )
    from .windows_process import ProcessIdentity, same_process

    alive = (
        type(pid) is int
        and pid > 0
        and type(created) is int
        and created > 0
        and isinstance(image, str)
        and bool(image)
        and same_process(ProcessIdentity(pid, Path(image), created))
    )
    return {
        "component": kind + "-connector",
        "task_name": tasks.TASK_NAMES[kind],
        "registered": xml is not None,
        "task_valid": valid,
        "enabled": enabled,
        "task_error": reason,
        "manifest_installed": manifest is not None,
        "owner_alive": bool(alive),
        "owner_pid": pid if alive else None,
        "core_listener_proof": "not_verified",
    }


def pause(kind: str) -> dict[str, Any]:
    """Disable future starts; do not force-end a serving connector session."""
    _owned(kind)
    _expect("/Change", "/TN", tasks.TASK_NAMES[kind], "/DISABLE")
    current = _registered(kind)
    if current is None or _enabled(current):
        raise ConnectorControlError("connector task did not become disabled")
    return status(kind)


def _wait_owner(kind: str, *, alive: bool, timeout_seconds: float = 20.0) -> dict[str, Any]:
    """Wait for a measured connector owner; never treat /Run as service proof."""
    import time

    deadline = time.monotonic() + timeout_seconds
    while True:
        snapshot = status(kind)
        if not snapshot["task_valid"]:
            raise ConnectorControlError("connector task contract changed during lifecycle action")
        if snapshot["owner_alive"] == alive:
            if not alive:
                _previous_gone(kind)
            return snapshot
        if time.monotonic() >= deadline:
            raise ConnectorControlError("connector lifecycle identity proof timed out")
        time.sleep(0.2)


def start(kind: str) -> dict[str, Any]:
    """Start one connector only after its Core policy and task are verified."""
    _require_kind(kind)
    _manifest, registered = _owned(kind)
    snapshot = status(kind)
    if snapshot["owner_alive"]:
        if not _enabled(registered):
            _expect("/Change", "/TN", tasks.TASK_NAMES[kind], "/ENABLE")
        return _wait_owner(kind, alive=True)

    _previous_gone(kind)
    policy = runtime.load_policy(kind)
    runtime.validate_runtime_inputs(kind, policy)
    # The registered policy digest has already been verified by _owned;
    # requiring Core listener ownership prevents a connector targeting a
    # foreign/absent Core. Connector failures never restart Core or Bridge.
    from .windows_core_listener import prove_core_listener

    prove_core_listener(
        expected_port=policy["core_port"],
        expected_hostname=policy["core_hostname"],
    )
    enabled_here = not _enabled(registered)
    if enabled_here:
        _expect("/Change", "/TN", tasks.TASK_NAMES[kind], "/ENABLE")
    try:
        _, registered = _owned(kind)
        if not _enabled(registered):
            raise ConnectorControlError("connector task did not become enabled")
        _expect("/Run", "/TN", tasks.TASK_NAMES[kind])
        return _wait_owner(kind, alive=True)
    except Exception:
        # Do not leave an unverified connector armed for the 1-minute trigger.
        # Only reverse our own enable if the exact manifest still owns the task.
        if enabled_here:
            try:
                _, current = _owned(kind)
                if _enabled(current):
                    _expect("/Change", "/TN", tasks.TASK_NAMES[kind], "/DISABLE")
            except Exception as rollback_error:
                raise ConnectorControlError(
                    "connector activation failed; rollback incomplete"
                ) from rollback_error
        raise


def stop(kind: str) -> dict[str, Any]:
    """Stop this connector task only; never end its independent Core or Bridge."""
    _require_kind(kind)
    _, registered = _owned(kind)
    if _enabled(registered):
        _expect("/Change", "/TN", tasks.TASK_NAMES[kind], "/DISABLE")
        _, registered = _owned(kind)
        if _enabled(registered):
            raise ConnectorControlError("connector task did not remain disabled")
    snapshot = status(kind)
    if snapshot["owner_alive"]:
        if _call("/End", "/TN", tasks.TASK_NAMES[kind]).returncode != 0:
            if status(kind)["owner_alive"]:
                raise ConnectorControlError("connector Task Scheduler could not end its owner")
        return _wait_owner(kind, alive=False)
    if not _pristine(kind):
        _previous_gone(kind)
    return status(kind)


def restart(kind: str) -> dict[str, Any]:
    stop(kind)
    return start(kind)


def uninstall_offline(kind: str) -> None:
    _, xml = _owned(kind)
    if _enabled(xml) or not _pristine(kind):
        raise ConnectorControlError("connector offline removal requires never-started task")
    _expect("/Delete", "/TN", tasks.TASK_NAMES[kind], "/F")
    if _registered(kind, optional=True) is not None:
        raise ConnectorControlError("connector registered task remains after deletion")
    for path in (
        runtime.task_path(kind),
        runtime.manifest_path(kind),
        runtime.state_path(kind),
    ):
        runtime._check_private(path, directory=False)
        path.unlink()

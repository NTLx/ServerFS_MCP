"""Single owner for fresh v25 Windows Task Scheduler component lifecycles.

No old W2 service controllers, generation state, proc-image equality or
in-band Tunnel->Core->Bridge supervisor calls are reachable from here.
Task Scheduler is authoritative; readiness is a separate functional probe.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from serverfs_mcp.windows_component_task import (
    NAMESPACE,
    ComponentTask,
    build_xml,
    validate_registered,
)

from . import storage
from .spec import COMPONENTS, Manifest

_NOT_FOUND = frozenset({0x80070002, 0x80070003})


class TaskError(Exception):
    """Redacted, component-scoped native lifecycle refusal."""


@dataclass(frozen=True)
class TaskRecord:
    state: str
    enabled: bool
    registered: bool = True

    @property
    def running(self) -> bool:
        return self.state == "Running"

    def status(self, component: str, task_name: str | None = None) -> dict[str, bool | str]:
        return {
            "component": component,
            "task_name": task_name or COMPONENTS[component],
            "registered": self.registered,
            "enabled": self.enabled,
            "state": self.state,
            "running": self.running,
            "readiness": "not_verified",
        }


def _task_spec(manifest: Manifest) -> ComponentTask:
    executable = (
        storage.pinned_pythonw(manifest.component)
        if manifest.instance_id == "default"
        else storage.pinned_pythonw(manifest.component, manifest.instance_id)
    )
    return ComponentTask(
        name=manifest.task_name(),
        sid=manifest.sid,
        executable=executable,
        arguments=manifest.task_argv(),
        enabled=False,
    )


def _enabled(xml: str) -> bool:
    """Read Scheduler's enabled setting, including its XML-schema default.

    Task Scheduler may omit an enabled task's ``Settings/Enabled`` after
    ``schtasks /Change /ENABLE``. The Windows Task Scheduler XSD defines
    that optional element with default=true; an explicit false is required
    for disabled tasks. Reject malformed/duplicate values rather than
    assuming a broken definition is enabled.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise TaskError("Scheduler task XML cannot be parsed") from exc
    settings = root.findall(f"./{{{NAMESPACE}}}Settings")
    if len(settings) != 1:
        raise TaskError("Scheduler task settings are ambiguous")
    nodes = settings[0].findall(f"./{{{NAMESPACE}}}Enabled")
    if not nodes:
        return True  # Microsoft Task Scheduler XSD: default="true", minOccurs=0.
    if len(nodes) != 1 or nodes[0].text is None:
        raise TaskError("Scheduler task enabled state is ambiguous")
    value = nodes[0].text.strip().lower()
    if value not in ("true", "false", "1", "0"):
        raise TaskError("Scheduler task enabled state is ambiguous")
    return value in ("true", "1")


def _decode_xml(raw: bytes) -> str:
    if not raw:
        raise TaskError("Scheduler returned an empty task definition")
    from serverfs_mcp.windows_service import _decode_process_output

    text = _decode_process_output(raw).lstrip("\ufeff\x00 \r\n\t")
    try:
        ET.fromstring(text)
    except ET.ParseError as exc:
        raise TaskError("Scheduler returned invalid XML") from exc
    return text


def _platform() -> None:
    if os.name != "nt" or sys.platform != "win32":
        raise TaskError("native Windows service requires Windows")


def _run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    """One fixed Windows executable and fixed arguments. Never a shell."""
    try:
        # pythonw.exe owns the v25 components. On Windows an otherwise
        # harmless Scheduler status probe spawns a visible PowerShell console
        # every few seconds unless CREATE_NO_WINDOW is set explicitly.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.run(
            argv,
            capture_output=True,
            timeout=25,
            check=False,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TaskError("Windows Task Scheduler command could not complete") from exc


class Scheduler:
    """Fixed task names, exact SID/XML contracts and OS task state."""

    def __init__(self, *, runner=_run) -> None:
        self._run = runner

    def _schtasks(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        from serverfs_mcp.windows_service import _schtasks_executable

        return self._run([str(_schtasks_executable()), *args])

    def _task_xml(self, manifest: Manifest, *, missing_ok: bool = False) -> str | None:
        result = self._schtasks("/Query", "/TN", manifest.task_name(), "/XML", "/HResult")
        if result.returncode != 0:
            if missing_ok and result.returncode & 0xFFFFFFFF in _NOT_FOUND:
                return None
            raise TaskError("fixed Scheduler task query failed")
        xml = _decode_xml(result.stdout)
        valid, _reason = validate_registered(xml, _task_spec(manifest))
        if not valid:
            raise TaskError("registered service task does not match owner contract")
        return xml

    def _task_state(self, manifest: Manifest) -> str:
        """Read the OS task state, never infer it from a PID file or old task."""
        # Instance IDs are strict ASCII slugs validated by Manifest.parse.
        # This is never an arbitrary PowerShell expression or shell command.
        name = manifest.task_name()
        command = (
            "$ErrorActionPreference='Stop';"
            f"$items=@(Get-ScheduledTask -TaskPath '\\' -TaskName '{name}');"
            "if($items.Count -ne 1){exit 7};"
            "[Console]::Out.Write($items[0].State.ToString())"
        )
        result = self._run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command])
        if result.returncode != 0:
            raise TaskError("Scheduler current-user state query is unavailable")
        state = result.stdout.decode("ascii", errors="replace").strip()
        if state not in {"Ready", "Running", "Disabled", "Queued"}:
            raise TaskError("Scheduler returned an unknown task state")
        return state

    def _record(self, manifest: Manifest, *, missing_ok: bool = False) -> TaskRecord | None:
        xml = self._task_xml(manifest, missing_ok=missing_ok)
        if xml is None:
            return None
        enabled = _enabled(xml)
        state = self._task_state(manifest)
        if enabled and state == "Disabled" or not enabled and state in {"Ready", "Queued"}:
            raise TaskError("Scheduler enabled and operational states disagree")
        return TaskRecord(state=state, enabled=enabled)

    def _change(self, manifest: Manifest, option: str) -> None:
        if option not in ("/ENABLE", "/DISABLE"):
            raise TaskError("invalid fixed service transition")
        result = self._schtasks("/Change", "/TN", manifest.task_name(), option)
        if result.returncode != 0:
            raise TaskError("Scheduler failed to change component enabled state")

    def status(self, manifest: Manifest) -> dict[str, bool | str]:
        record = self._record(manifest, missing_ok=True)
        if record is None:
            return {
                "component": manifest.component,
                "task_name": manifest.task_name(),
                "registered": False,
                "enabled": False,
                "state": "Absent",
                "running": False,
                "readiness": "not_verified",
            }
        response = record.status(manifest.component, manifest.task_name())
        if record.running:
            from .readiness import status_readiness

            try:
                response["readiness"] = status_readiness(manifest, task_running=True)
            except Exception:  # noqa: BLE001 - task state is not health
                response["readiness"] = "not_verified"
        else:
            response["readiness"] = "offline"
        return response

    def install(self, manifest: Manifest) -> dict[str, bool | str]:
        _platform()
        if self._record(manifest, missing_ok=True) is not None:
            raise TaskError("component already registered; install refuses replacement")
        from serverfs_mcp.windows_private_artifacts import protect_new_file

        sid = manifest.sid
        root = (
            storage.home(manifest.component, manifest.instance_id, manifest.connector_id)
            if manifest.connector_id is not None
            else storage.home(manifest.component, manifest.instance_id)
            if manifest.instance_id != "default"
            else storage.home(manifest.component)
        )
        storage.check_private(root, directory=True, sid=sid)
        filename = root / "task.xml"
        if filename.exists() or filename.is_symlink():
            # A failed first /Create must be safely retryable. Never replace
            # or adopt a foreign, unprotected or enabled task definition.
            storage.check_private(filename, directory=False, sid=sid)
            try:
                staged = _decode_xml(filename.read_bytes())
            except OSError as exc:
                raise TaskError("staged Scheduler definition is unreadable") from exc
            valid, _reason = validate_registered(staged, _task_spec(manifest))
            if not valid or _enabled(staged):
                raise TaskError("staged Scheduler definition does not match this owner")
        else:
            xml = build_xml(_task_spec(manifest))
            text = _decode_xml(xml)
            valid, _reason = validate_registered(text, _task_spec(manifest))
            if not valid or _enabled(text):
                raise TaskError("new Scheduler XML is not an owned disabled task")
            temp = root / ".task-new.xml"
            if temp.exists() or temp.is_symlink():
                raise TaskError("another task staging operation is present")
            try:
                with temp.open("xb") as stream:
                    stream.write(xml)
                    stream.flush()
                    os.fsync(stream.fileno())
                protect_new_file(temp, sid)
                storage.check_private(temp, directory=False, sid=sid)
                os.link(temp, filename)
            finally:
                temp.unlink(missing_ok=True)
            storage.check_private(filename, directory=False, sid=sid)
        result = self._schtasks("/Create", "/TN", manifest.task_name(), "/XML", str(filename))
        if result.returncode != 0:
            raise TaskError("Scheduler disabled installation failed; preserve staged XML")
        record = self._record(manifest)
        if record is None or record.enabled or record.running:
            raise TaskError("new component was not registered Disabled")
        return record.status(manifest.component, manifest.task_name())

    def start(self, manifest: Manifest) -> dict[str, bool | str]:
        """Enable/run ONE task. Fail without enabling a different component."""
        _platform()
        current = self._record(manifest)
        if current is None:
            raise TaskError("component task is absent")
        if current.running and current.enabled:
            # A running Scheduler task is not proof that its Core/owner is
            # healthy. Idempotent start must not report a false success.
            response = self.status(manifest)
            expected = "verified" if manifest.component == "core" else "owner_alive"
            if response["readiness"] != expected:
                raise TaskError("component already running but readiness is unverified")
            return response
        if current.running and not current.enabled:
            raise TaskError("component task running while disabled; refuse auto-recovery")
        if current.state not in ("Disabled", "Ready"):
            raise TaskError("component task is not in an activatable state")
        newly_enabled = not current.enabled
        stage = "enable"
        try:
            if newly_enabled:
                self._change(manifest, "/ENABLE")
            stage = "run"
            verified = self._record(manifest)
            if verified is None or not verified.enabled:
                raise TaskError("enabled Scheduler task identity cannot be verified")
            # Logon/time triggers can start this task immediately upon
            # /ENABLE. Never issue a second /Run to an already Running owner;
            # IgnoreNew may legitimately return 0x800710E0 in that race.
            if not verified.running:
                started = self._schtasks("/Run", "/TN", manifest.task_name())
                if started.returncode != 0:
                    # A concurrent trigger is acceptable only if Scheduler
                    # now confirms the exact registered task is running.
                    observed = self._record(manifest)
                    if observed is None or not observed.enabled or not observed.running:
                        raise TaskError("Scheduler refused to run the new component")
            stage = "ready"
            deadline = time.monotonic() + 15
            while True:
                response = self.status(manifest)
                if response["running"] is True and response["readiness"] == (
                    "verified" if manifest.component == "core" else "owner_alive"
                ):
                    return response
                if time.monotonic() >= deadline:
                    raise TaskError("new service failed its independent readiness check")
                time.sleep(0.25)
        except Exception as exc:
            # A failed start must leave no auto-startable owner, even when
            # this exact task was already Enabled/Ready on entry. Recheck
            # ownership before disabling; never kill a PID or a peer service.
            rollback = "UNVERIFIED"
            try:
                self._task_xml(manifest)
                self.stop(manifest)
                rollback = "disabled-and-ended"
            except Exception:  # noqa: BLE001 - unknown task state preserved
                pass
            raise TaskError(f"component start refused at {stage}; rollback={rollback}") from exc

    def stop(self, manifest: Manifest) -> dict[str, bool | str]:
        """Disable future starts then end ONLY this known TaskScheduler task."""
        _platform()
        previous = self._record(manifest)
        if previous is None:
            raise TaskError("component task is not installed")
        if previous.enabled:
            self._change(manifest, "/DISABLE")
        # /End is only permitted for a task the OS still reports Running.
        current = self._record(manifest)
        if current is None or current.enabled:
            raise TaskError("component disable could not be verified")
        if current.running:
            if self._schtasks("/End", "/TN", manifest.task_name()).returncode != 0:
                raise TaskError("component task could not be ended")
        # Task Scheduler can report Disabled/not Running before pythonw.exe's
        # previous kernel generation has fully exited. Poll both authorities
        # until they agree; never confuse that transient with a failed stop.
        from .readiness import _current_identity

        deadline = time.monotonic() + 12
        while True:
            state = self._record(manifest)
            if state is None or state.enabled:
                raise TaskError("component stop task registration changed")
            if not state.running and _current_identity(manifest) is None:
                return state.status(manifest.component, manifest.task_name())
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        raise TaskError("component stop is unverified; no direct PID termination")

    def uninstall(self, manifest: Manifest) -> dict[str, bool | str]:
        """Uninstall only a fully disabled/offline new service, retain evidence."""
        _platform()
        record = self._record(manifest)
        if record is None or record.enabled or record.running:
            raise TaskError("component removal requires Disabled/offline task")
        if self._schtasks("/Delete", "/TN", manifest.task_name(), "/F").returncode != 0:
            raise TaskError("component task deletion failed")
        if self._task_xml(manifest, missing_ok=True) is not None:
            raise TaskError("component task still registered after deletion")
        return self.status(manifest)

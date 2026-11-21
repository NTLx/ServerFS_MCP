"""Conservative component-scoped Bridge task control (v0.25.0 W1b-3).

Source-only, deliberately not wired into serverfs CLI. The fixed Bridge task
is distinct from the two live legacy ingress tasks. Installation is DISABLED.
A running Agent Bridge may own active providers and a retained recovery guard:
this module MUST NOT kill it or treat empty task count as quiescence.

Supported staging/control: fresh disabled install, status, disable future
starts, offline-only removal. Active stop, restart and enabling a new owner
require later generation-bound, provider-safe W2/W3 handover proof.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import windows_bridge_service as runtime
from . import windows_bridge_task_spec as contract

# These are existing Windows Task Scheduler HRESULTs, NOT localized stderr.
_TASK_NOT_FOUND = frozenset({0x80070002, 0x80070003})
_REPARSE_POINT = 0x00000400


class BridgeControlError(Exception):
    """A fixed operator-safe refusal with no secret or user-path payload."""


def _ensure_windows() -> None:
    runtime._ensure_windows()


def _check_regular(path: Path, *, directory: bool) -> None:
    """Reject links, junctions and other reparse points before touching files."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise BridgeControlError("Bridge service storage is inaccessible") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & _REPARSE_POINT or stat.S_ISLNK(info.st_mode):
        raise BridgeControlError("Bridge service storage redirects to another location")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise BridgeControlError("Bridge service directory is not regular")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise BridgeControlError("Bridge service artifact is not regular")


def _check_private_home(*, create: bool = False) -> Path:
    """Use the dedicated current-user LocalAppData tree, never a workdir root."""
    _ensure_windows()
    from .native_lifecycle import window_local_app_data

    known_home = window_local_app_data()
    expected = known_home / "ServerFS" / "service-manager" / "windows" / "agent-bridge"
    if not runtime._canonical_match(runtime.service_home(), expected):
        raise BridgeControlError("Bridge service home is outside its fixed user scope")
    if create:
        # A new directory inherits the caller's ambient DACL. The service
        # controller must NEVER silently create this private service home.
        raise BridgeControlError("Bridge service home requires private pre-provisioning")
    # Include existing intermediates, not only the final component. A Windows
    # junction inside the prefix defeats an otherwise regular-looking leaf.
    parents = [expected, *expected.parents]
    for entry in reversed(parents):
        if not entry.exists() and not entry.is_symlink():
            continue
        _check_regular(entry, directory=True)
    from .windows_core_control import _check_private

    _check_private(expected, directory=True, sid=_current_sid())
    return expected


def _exclude_exposed_workdirs(config_path: Path, home: Path) -> None:
    """Reject any service home nested inside an exposed MCP workdir (or vice versa)."""
    from .native_config import load_native_config

    try:
        workdirs, settings = load_native_config(config_path)
    except Exception as exc:  # noqa: BLE001 - native parser messages may include paths
        raise BridgeControlError("Bridge native configuration is invalid") from exc
    if not settings.bridge_required:
        raise BridgeControlError("Bridge policy does not enable Agent or Repo Ops")
    private = home.resolve()
    for item in workdirs:
        public = item.root.resolve()
        if private == public or private.is_relative_to(public) or public.is_relative_to(private):
            raise BridgeControlError("Bridge service state overlaps an exposed workdir")


def _current_sid() -> str:
    from .native_endpoint import current_user_sid

    try:
        return current_user_sid()
    except Exception as exc:  # noqa: BLE001 - do not report raw Windows token details
        raise BridgeControlError("current Windows user SID is unavailable") from exc


def _dedicated_pythonw() -> Path:
    """This component is separately installed, not pinned to a Connector's env."""
    from .native_lifecycle import window_local_app_data

    prefix = (window_local_app_data() / "ServerFS" / "bridge-owner-env").resolve()
    if not runtime._canonical_match(Path(sys.prefix).resolve(), prefix):
        raise BridgeControlError("Bridge task staging requires its dedicated bridge-owner-env")
    executable = prefix / "Scripts" / "pythonw.exe"
    from .windows_core_control import _check_private

    sid = _current_sid()
    for folder in (
        prefix,
        prefix / "Scripts",
        prefix / "Lib",
        prefix / "Lib" / "site-packages",
        prefix / "Lib" / "site-packages" / "serverfs_mcp",
    ):
        _check_private(folder, directory=True, sid=sid)
    _check_private(executable, directory=False, sid=sid)
    return executable


def _ensure_backend(spec: contract.BridgeTaskSpec) -> None:
    """Check both isolated installs without importing the Bridge SDK in the owner."""
    from .windows_service import _validate_runtime_backend

    try:
        _validate_runtime_backend()  # Product+Windows Native belong to Owner.
        bridge_executable = runtime._verified_bridge_interpreter(
            {
                "user_sid": spec.user_sid,
                "task_executable": str(spec.task_executable),
                "bridge_executable": str(spec.bridge_executable()),
            }
        )
        # The child can import the separately installed Bridge wheel+SDKs.
        # Use -I/-B and suppress all output; never inherit ambient Tunnel
        # credentials or permit an injected SERVERFS_BRIDGE_PYTHON.
        probe = subprocess.run(
            [
                str(bridge_executable),
                "-I",
                "-B",
                "-c",
                "import serverfs_agent_bridge.main",
            ],
            env=runtime._runtime_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if probe.returncode != 0:
            raise BridgeControlError("Bridge runtime installed package import failed")
    except Exception as exc:  # noqa: BLE001 - dependencies are an installation gate
        raise BridgeControlError(
            "Bridge owner or isolated Bridge runtime packages are unavailable"
        ) from exc


def _schtasks(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    """Never accept a task name from the caller or use a shell."""
    from .windows_service import _schtasks_executable

    try:
        return subprocess.run(
            [str(_schtasks_executable()), *arguments],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeControlError("Bridge Task Scheduler operation is unavailable") from exc


def _registered_xml(*, optional: bool = False) -> str | None:
    """A missing task is different from a failed/denied Scheduler query."""
    from .windows_service import _decode_process_output

    result = _schtasks("/Query", "/TN", contract.TASK_NAME, "/XML", "/HResult")
    if result.returncode == 0:
        xml = _decode_process_output(result.stdout).lstrip("\ufeff\x00 \t\r\n")
        if not xml:
            raise BridgeControlError("Bridge registered task XML is empty")
        return xml
    if result.returncode & 0xFFFFFFFF in _TASK_NOT_FOUND:
        if optional:
            return None
        raise BridgeControlError("Bridge task is not registered")
    raise BridgeControlError("Bridge task query failed")


def _expect_success(*args: str) -> None:
    if _schtasks(*args).returncode != 0:
        raise BridgeControlError("Bridge Task Scheduler operation failed")


def _atomic_write(path: Path, content: bytes) -> None:
    """Only exact Bridge-owned artifacts; exclusive creation, no secret values."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        from .windows_core_control import _check_private
        from .windows_private_artifacts import protect_new_file

        sid = _current_sid()
        protect_new_file(tmp, sid)
        _check_private(tmp, directory=False, sid=sid)
        # A same-volume hard link is create-only, unlike os.replace() which
        # could overwrite a file created by a concurrent staging attempt.
        os.link(tmp, path)
    except Exception as exc:  # noqa: BLE001 - no private paths in diagnostics
        raise BridgeControlError("Bridge service artifact could not be written") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _stage_files(spec: contract.BridgeTaskSpec) -> None:
    """Produce a disabled, authored XML and exact manifest; no OS task starts."""
    xml = contract.build_task_xml(spec)
    manifest = contract.build_manifest(spec, xml)
    good, _ = contract.validate_authored_task_xml(xml, manifest)
    if not good:
        raise BridgeControlError("Bridge task source contract did not validate")
    _atomic_write(runtime.authored_task_path(), xml)
    _atomic_write(
        runtime.manifest_path(),
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def install_disabled(config_path: Path) -> dict[str, Any]:
    """Register only a NEW disabled Bridge task. Never overwrite an existing task.

    No /F on Create: a concurrent foreign task cannot be replaced. The routine
    rolls back its exact new files and an exactly created task on failure.
    Nothing in an already running Tunnel/Cloudflare task is queried or touched.
    """
    _ensure_windows()
    try:
        config = config_path.resolve(strict=True)
    except OSError as exc:
        raise BridgeControlError("Bridge policy configuration is unavailable") from exc
    _exclude_exposed_workdirs(config, runtime.service_home())
    executable = _dedicated_pythonw()
    if _registered_xml(optional=True) is not None:
        raise BridgeControlError("Bridge task already exists; refusing replacement")
    if any(
        path.exists() or path.is_symlink()
        for path in (
            runtime.manifest_path(),
            runtime.authored_task_path(),
            runtime.watchdog_state_path(),
        )
    ):
        raise BridgeControlError("Bridge staged artifacts already exist")
    _check_private_home()
    spec = contract.BridgeTaskSpec(_current_sid(), executable, config, enabled=False).validated()
    _ensure_backend(spec)
    created = False
    try:
        _stage_files(spec)
        # A later owner launch MUST replace this state before attempting a
        # Runner. Staged-only uninstall is permitted only with this evidence.
        staged = {
            "schema_version": runtime.STATE_SCHEMA_VERSION,
            "component": "agent-bridge",
            "stage": "disabled-never-started",
            "runner_restart_count": 0,
            "watchdog_pid": None,
            "managed_runner_pid": None,
        }
        _atomic_write(
            runtime.watchdog_state_path(),
            json.dumps(staged, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        # Explicitly omit /F; this cannot overwrite a task that raced in.
        _expect_success(
            "/Create", "/TN", contract.TASK_NAME, "/XML", str(runtime.authored_task_path())
        )
        created = True
        actual = _registered_xml()
        assert actual is not None
        manifest = runtime._read_manifest()
        good, _ = contract.validate_registered_task(actual, manifest)
        if not good:
            raise BridgeControlError("Bridge registered task differs from staged contract")
        if _task_enabled(actual):
            raise BridgeControlError("Bridge newly registered task was unexpectedly enabled")
        snapshot = runtime.status()
        if not (
            snapshot.get("registered") is True
            and snapshot.get("task_valid") is True
            and snapshot.get("enabled") is False
        ):
            raise BridgeControlError("Bridge task status disagrees with registered contract")
        return snapshot
    except Exception as original:
        rollback_problem = False
        may_remove_files = False
        if not created:
            # A failed /Create can still have registered a task before
            # returning an error. Never remove the manifest until absence
            # has been independently confirmed; never delete unknown tasks.
            try:
                may_remove_files = _registered_xml(optional=True) is None
            except BridgeControlError:
                rollback_problem = True
        if created:
            try:
                # Never delete a task which is now enabled, drifted, or whose
                # owner has started. It might already own live providers.
                current = _registered_xml()
                manifest = runtime._read_manifest()
                staged_state = runtime._read_state(runtime.watchdog_state_path())
                valid, _ = contract.validate_registered_task(current or "", manifest)
                never_started = staged_state.get("stage") == "disabled-never-started"
                if not valid or current is None or _task_enabled(current) or not never_started:
                    raise BridgeControlError(
                        "Bridge rollback cannot prove a never-started disabled owner"
                    )
                _expect_success("/Delete", "/TN", contract.TASK_NAME, "/F")
                may_remove_files = _registered_xml(optional=True) is None
            except Exception:  # noqa: BLE001 - preserve unknown running/foreign task
                rollback_problem = True
        if may_remove_files:
            for path in (
                runtime.manifest_path(),
                runtime.authored_task_path(),
                runtime.watchdog_state_path(),
            ):
                try:
                    if path.exists():
                        _check_regular(path, directory=False)
                        path.unlink()
                except (OSError, BridgeControlError):
                    rollback_problem = True
        else:
            rollback_problem = True
        if rollback_problem:
            raise BridgeControlError("Bridge install failed; rollback incomplete") from original
        raise


def _task_enabled(xml: str) -> bool:
    from xml.etree import ElementTree as ET

    try:
        element = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise BridgeControlError("Bridge task XML is invalid") from exc
    raw = element.findtext(
        "./t:Settings/t:Enabled",
        default="true",
        namespaces={"t": contract.TASK_XML_NAMESPACE},
    )
    return raw.strip().lower() != "false"


def _require_owned() -> tuple[dict[str, Any], str]:
    _check_private_home()
    manifest = runtime._read_manifest()
    if manifest["user_sid"] != _current_sid():
        raise BridgeControlError("Bridge staged task belongs to another user")
    source = runtime.authored_task_path()
    _check_regular(source, directory=False)
    from .windows_core_control import _check_private

    _check_private(source, directory=False, sid=_current_sid())
    good, _ = contract.validate_authored_task_xml(source.read_bytes(), manifest)
    if not good:
        raise BridgeControlError("Bridge authored task is invalid")
    xml = _registered_xml()
    if xml is None:
        raise BridgeControlError("Bridge task is not registered")
    good, _ = contract.validate_registered_task(xml, manifest)
    if not good:
        raise BridgeControlError("Bridge registered task contract has drifted")
    return manifest, xml


def status() -> dict[str, Any]:
    """Read-only component report; this never mutates Task Scheduler or providers."""
    return runtime.status()


def pause() -> dict[str, Any]:
    """Disable future starts, but DO NOT end a running Bridge or its providers.

    A paused task may still be actively serving. Never claim otherwise.
    """
    _require_owned()
    _expect_success("/Change", "/TN", contract.TASK_NAME, "/DISABLE")
    xml = _registered_xml()
    if xml is None or _task_enabled(xml):
        raise BridgeControlError("Bridge task did not become disabled")
    return status()


def _assert_idle() -> None:
    """A missing/untrusted identity is NOT sufficient proof of provider quiescence."""
    state = runtime._read_state(runtime.watchdog_state_path())
    # A stopped *process* is not proof that an ever-started Bridge's provider
    # tree and recovery guard are reconciled. W1b-3 permits removal only of an
    # installation that has NEVER entered an owned generation.
    if (
        state.get("component") != "agent-bridge"
        or state.get("stage") != "disabled-never-started"
        or type(state.get("runner_restart_count")) is not int
        or state["runner_restart_count"] != 0
        or state.get("watchdog_pid") is not None
        or state.get("managed_runner_pid") is not None
    ):
        raise BridgeControlError("Bridge owner has started or its history is unverified")
    if runtime._identity_alive(state, "watchdog"):
        raise BridgeControlError("Bridge watchdog remains active")
    if runtime._identity_alive(state, "managed_runner"):
        raise BridgeControlError("Bridge owner runner remains active")


def stop() -> dict[str, Any]:
    """Only an already-idle task may be fully stopped at this W1b source stage.

    Stopping an active owner could kill providers; proof of no active Agent
    tasks and recovery guard reconciliation belongs to W2. For an active
    service, use pause() to prevent new starts without touching its process.
    """
    _, xml = _require_owned()
    if _task_enabled(xml):
        raise BridgeControlError("disable the Bridge task before offline stop")
    _assert_idle()
    return status()


def restart() -> dict[str, Any]:
    raise BridgeControlError("Bridge restart requires W2 provider-safe handover proof")


def start() -> dict[str, Any]:
    raise BridgeControlError("Bridge start requires W2 generation/legacy owner isolation proof")


def uninstall_offline() -> None:
    """Remove only a never-started disabled task; preserve logs and provider state."""
    _, xml = _require_owned()
    if _task_enabled(xml):
        raise BridgeControlError("disable Bridge task before offline uninstall")
    _assert_idle()
    _expect_success("/Delete", "/TN", contract.TASK_NAME, "/F")
    if _registered_xml(optional=True) is not None:
        raise BridgeControlError("Bridge task persists after deletion")
    for path in (
        runtime.authored_task_path(),
        runtime.manifest_path(),
        runtime.watchdog_state_path(),
    ):
        _check_regular(path, directory=False)
        path.unlink()

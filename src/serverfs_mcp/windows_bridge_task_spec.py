"""Pure Windows Task Scheduler contract for the independent Agent Bridge (v0.25.0 W1b).

Source-level only. No registration, process creation, secret reads or service commands.
The fixed task identity and component-scoped arguments cannot target legacy Tunnel
or Cloudflare tasks. W1b service implementation must validate the registered XML
against this contract before any lifecycle action.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

TASK_NAME = "ServerFS Agent Bridge"
TASK_URI = "\\" + TASK_NAME
TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
MANIFEST_SCHEMA_VERSION = 2
SCHEDULER_REPETITION_INTERVAL = "PT1M"
SCHEDULER_RESTART_INTERVAL = "PT1M"
SCHEDULER_RESTART_COUNT = 255
_SID_PATTERN = re.compile(r"S-1-(?:[0-9]+-)+[0-9]+\Z")


class BridgeTaskContractError(ValueError):
    """An operator-safe, fixed-message task contract refusal."""


@dataclass(frozen=True)
class BridgeTaskSpec:
    """One same-user task, disabled by default until its owner is safely staged."""

    user_sid: str
    task_executable: Path
    config_path: Path
    enabled: bool = False

    def validated(self) -> BridgeTaskSpec:
        if not _SID_PATTERN.fullmatch(self.user_sid):
            raise BridgeTaskContractError("Bridge task user SID is invalid")
        if not self.task_executable.is_absolute() or not self.config_path.is_absolute():
            raise BridgeTaskContractError("Bridge task paths must be absolute")
        if not isinstance(self.enabled, bool):
            raise BridgeTaskContractError("Bridge task enabled flag must be boolean")
        if self.task_executable.name.casefold() != "pythonw.exe":
            raise BridgeTaskContractError("Bridge task executable must be pythonw.exe")
        return self

    def bridge_executable(self) -> Path:
        """Independent, non-owner Bridge runtime; never inherited from an environment variable.

        The owner is installed in .../ServerFS/bridge-owner-env/Scripts/pythonw.exe.
        The Bridge wheel and provider SDKs live under a sibling protected environment.
        """
        self.validated()
        return (
            self.task_executable.parent.parent.parent
            / "bridge-runtime-env"
            / "Scripts"
            / "python.exe"
        )

    def arguments(self) -> list[str]:
        self.validated()
        return [
            "-I",  # Ignore PYTHONPATH/user site and unsafe cwd during service bootstrap.
            "-m",
            "serverfs_mcp.windows_bridge_service",
            "_watchdog",
            "--config",
            str(self.config_path),
        ]


def _tag(name: str) -> str:
    return f"{{{TASK_XML_NAMESPACE}}}{name}"


def _add(parent: ET.Element, name: str, value: str | None = None, **attrs: str) -> ET.Element:
    child = ET.SubElement(parent, _tag(name), attrs)
    if value is not None:
        child.text = value
    return child


def build_task_xml(spec: BridgeTaskSpec, *, start_boundary: str | None = None) -> bytes:
    """Render the existing current-user/least-privilege heartbeat contract.

    No credentials or caller-supplied commands are represented in the Task XML.
    In particular, the task always starts its own Bridge watchdog entrypoint,
    never serverfs_mcp.supervisor, a Tunnel, HTTP Core, or a provider executable.
    """
    spec.validated()
    ET.register_namespace("", TASK_XML_NAMESPACE)
    task = ET.Element(_tag("Task"), {"version": "1.4"})
    registration = _add(task, "RegistrationInfo")
    _add(registration, "Author", spec.user_sid)
    _add(registration, "Description", "Independent current-user ServerFS Agent Bridge")
    _add(registration, "URI", TASK_URI)

    triggers = _add(task, "Triggers")
    logon = _add(triggers, "LogonTrigger")
    _add(logon, "Enabled", "true")
    _add(logon, "UserId", spec.user_sid)
    timer = _add(triggers, "TimeTrigger")
    repetition = _add(timer, "Repetition")
    _add(repetition, "Interval", SCHEDULER_REPETITION_INTERVAL)
    _add(repetition, "StopAtDurationEnd", "false")
    boundary = start_boundary or datetime.now().astimezone().replace(microsecond=0).isoformat()
    _add(timer, "StartBoundary", boundary)
    _add(timer, "Enabled", "true")

    principals = _add(task, "Principals")
    principal = _add(principals, "Principal", None, id="Author")
    _add(principal, "UserId", spec.user_sid)
    _add(principal, "LogonType", "InteractiveToken")
    _add(principal, "RunLevel", "LeastPrivilege")

    settings = _add(task, "Settings")
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true" if spec.enabled else "false"),
        ("Hidden", "false"),
        ("RunOnlyIfIdle", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT0S"),
        ("Priority", "7"),
    ):
        _add(settings, name, value)
    restart = _add(settings, "RestartOnFailure")
    _add(restart, "Interval", SCHEDULER_RESTART_INTERVAL)
    _add(restart, "Count", str(SCHEDULER_RESTART_COUNT))

    actions = _add(task, "Actions", None, Context="Author")
    action = _add(actions, "Exec")
    _add(action, "Command", str(spec.task_executable))
    _add(action, "Arguments", subprocess.list2cmdline(spec.arguments()))
    # Never import from a potentially writable/exposed config or workdir tree.
    _add(action, "WorkingDirectory", str(spec.task_executable.parent))
    body = ET.tostring(task, encoding="unicode", xml_declaration=False)
    return ('<?xml version="1.0" encoding="UTF-16"?>\r\n' + body).encode("utf-16")


def build_manifest(spec: BridgeTaskSpec, task_xml: bytes) -> dict[str, Any]:
    """Persist only the intended task identity and its exact authored XML hash."""
    spec.validated()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "component": "agent-bridge",
        "task_name": TASK_NAME,
        "task_uri": TASK_URI,
        "user_sid": spec.user_sid,
        "task_executable": str(spec.task_executable),
        "bridge_executable": str(spec.bridge_executable()),
        "task_arguments": spec.arguments(),
        "config_path": str(spec.config_path),
        "task_xml_sha256": hashlib.sha256(task_xml).hexdigest(),
    }


def validate_manifest(manifest: object) -> tuple[bool, str]:
    """Reject malformed/foreign manifest before acting on a registered task."""
    if not isinstance(manifest, dict):
        return False, "Bridge service manifest is invalid"
    try:
        sid = manifest["user_sid"]
        exe = manifest["task_executable"]
        config = manifest["config_path"]
        bridge_exe = manifest["bridge_executable"]
        if not all(isinstance(x, str) for x in (sid, exe, config, bridge_exe)):
            return False, "Bridge service manifest identity is invalid"
        spec = BridgeTaskSpec(sid, Path(exe), Path(config)).validated()
        # An enabled/disabled Task Scheduler registration is an OS state,
        # not a mutable authority encoded in a source manifest.
        baseline = build_task_xml(spec, start_boundary="2000-01-01T00:00:00+00:00")
        expected = build_manifest(spec, baseline)
        required = set(expected)
        if set(manifest) != required or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
            return False, "Bridge service manifest schema is invalid"
        for field in required - {"task_xml_sha256"}:
            if manifest[field] != expected[field]:
                return False, "Bridge service manifest contract mismatch"
        digest = manifest["task_xml_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False, "Bridge task XML digest is invalid"
        return True, ""
    except (KeyError, TypeError, ValueError, OSError):
        return False, "Bridge service manifest is invalid"


def validate_authored_task_xml(task_xml: bytes, manifest: object) -> tuple[bool, str]:
    """Verify the exact intended on-disk task XML before registering it.

    Task Scheduler normalizes XML after registration, so use the semantic
    validate_registered_task() check on the OS-returned XML instead.
    """
    valid, reason = validate_manifest(manifest)
    if not valid:
        return False, reason
    assert isinstance(manifest, dict)
    if hashlib.sha256(task_xml).hexdigest() != manifest["task_xml_sha256"]:
        return False, "authored Bridge task XML digest mismatch"
    try:
        return validate_registered_task(task_xml.decode("utf-16"), manifest)
    except (UnicodeError, ValueError):
        return False, "authored Bridge task XML is invalid"


def validate_registered_task(task_xml: str, manifest: object) -> tuple[bool, str]:
    """Check an OS-normalized task, not a byte-wise copy of the authored XML."""
    valid, reason = validate_manifest(manifest)
    if not valid:
        return False, reason
    assert isinstance(manifest, dict)
    try:
        root = ET.fromstring(task_xml)
    except ET.ParseError:
        return False, "registered Bridge task XML is invalid"
    if root.tag != _tag("Task"):
        return False, "registered Bridge task root is invalid"

    def field(path: str) -> str:
        child = root.find(path, {"t": TASK_XML_NAMESPACE})
        return "" if child is None or child.text is None else child.text.strip()

    def same_sid(value: str) -> bool:
        if value.casefold() == manifest["user_sid"].casefold():
            return True
        if sys.platform != "win32":
            return False
        # Task Scheduler may normalize a SID to DOMAIN\user. Reuse the
        # existing Win32 SID comparison instead of trusting a string alias.
        from .windows_service import _identity_matches_sid

        return _identity_matches_sid(value, manifest["user_sid"])

    checks = {
        "URI": field("./t:RegistrationInfo/t:URI") == TASK_URI,
        "principal SID": same_sid(field("./t:Principals/t:Principal/t:UserId")),
        "trigger SID": same_sid(field("./t:Triggers/t:LogonTrigger/t:UserId")),
        "logon token": field("./t:Principals/t:Principal/t:LogonType") == "InteractiveToken",
        "least privilege": (field("./t:Principals/t:Principal/t:RunLevel") or "LeastPrivilege")
        == "LeastPrivilege",
        "time heartbeat": field("./t:Triggers/t:TimeTrigger/t:Repetition/t:Interval")
        == SCHEDULER_REPETITION_INTERVAL,
        "no finite duration": not field("./t:Triggers/t:TimeTrigger/t:Repetition/t:Duration"),
        "valid heartbeat": bool(field("./t:Triggers/t:TimeTrigger/t:StartBoundary")),
        "single instance": field("./t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew",
        "no execution limit": field("./t:Settings/t:ExecutionTimeLimit") == "PT0S",
        "restart interval": field("./t:Settings/t:RestartOnFailure/t:Interval")
        == SCHEDULER_RESTART_INTERVAL,
        "restart count": field("./t:Settings/t:RestartOnFailure/t:Count")
        == str(SCHEDULER_RESTART_COUNT),
        "task executable": os.path.normcase(field("./t:Actions/t:Exec/t:Command"))
        == os.path.normcase(manifest["task_executable"]),
        "task arguments": field("./t:Actions/t:Exec/t:Arguments")
        == subprocess.list2cmdline(manifest["task_arguments"]),
        "task working directory": os.path.normcase(field("./t:Actions/t:Exec/t:WorkingDirectory"))
        == os.path.normcase(str(Path(manifest["task_executable"]).parent)),
        "exactly one Exec": len(root.findall("./t:Actions/t:Exec", {"t": TASK_XML_NAMESPACE})) == 1,
        "no foreign actions": len(root.findall("./t:Actions/*", {"t": TASK_XML_NAMESPACE})) == 1,
        "exactly two triggers": len(root.findall("./t:Triggers/*", {"t": TASK_XML_NAMESPACE})) == 2,
        "one principal": len(root.findall("./t:Principals/t:Principal", {"t": TASK_XML_NAMESPACE}))
        == 1,
        "logon trigger enabled": field("./t:Triggers/t:LogonTrigger/t:Enabled").lower() != "false",
        "heartbeat enabled": field("./t:Triggers/t:TimeTrigger/t:Enabled").lower() != "false",
        "heartbeat never stops": (
            field("./t:Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd").lower()
            in {"", "false"}
        ),
    }
    bad = [name for name, okay in checks.items() if not okay]
    return (False, "registered Bridge task mismatch: " + ", ".join(bad)) if bad else (True, "")

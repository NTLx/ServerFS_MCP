"""Small pure Task Scheduler 2.0 XML contract shared by new independent components.

This contains no schtasks calls, process lifecycle or credential access. Existing
production Windows tasks are not changed or migrated by importing this module.
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
REPETITION = "PT1M"
RESTART_INTERVAL = "PT1M"
RESTART_COUNT = "255"


@dataclass(frozen=True)
class ComponentTask:
    name: str
    sid: str
    executable: Path
    arguments: tuple[str, ...]
    enabled: bool = False

    @property
    def uri(self) -> str:
        return "\\" + self.name


def _tag(name: str) -> str:
    return f"{{{NAMESPACE}}}{name}"


def _field(parent: ET.Element, name: str, value: str | None = None, **attrs: str) -> ET.Element:
    child = ET.SubElement(parent, _tag(name), attrs)
    if value is not None:
        child.text = value
    return child


def build_xml(task: ComponentTask, *, boundary: str | None = None) -> bytes:
    """One current-user, least-privilege scheduled process; disabled by default."""
    ET.register_namespace("", NAMESPACE)
    root = ET.Element(_tag("Task"), {"version": "1.4"})
    registration = _field(root, "RegistrationInfo")
    _field(registration, "Author", task.sid)
    _field(registration, "Description", f"Independent ServerFS {task.name}")
    _field(registration, "URI", task.uri)
    triggers = _field(root, "Triggers")
    logon = _field(triggers, "LogonTrigger")
    _field(logon, "Enabled", "true")
    _field(logon, "UserId", task.sid)
    timer = _field(triggers, "TimeTrigger")
    repetition = _field(timer, "Repetition")
    _field(repetition, "Interval", REPETITION)
    _field(repetition, "StopAtDurationEnd", "false")
    start = boundary or datetime.now().astimezone().replace(microsecond=0).isoformat()
    _field(timer, "StartBoundary", start)
    _field(timer, "Enabled", "true")
    principals = _field(root, "Principals")
    principal = _field(principals, "Principal", None, id="Author")
    _field(principal, "UserId", task.sid)
    _field(principal, "LogonType", "InteractiveToken")
    _field(principal, "RunLevel", "LeastPrivilege")
    settings = _field(root, "Settings")
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true" if task.enabled else "false"),
        ("Hidden", "false"),
        ("RunOnlyIfIdle", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT0S"),
        ("Priority", "7"),
    ):
        _field(settings, name, value)
    restart = _field(settings, "RestartOnFailure")
    _field(restart, "Interval", RESTART_INTERVAL)
    _field(restart, "Count", RESTART_COUNT)
    actions = _field(root, "Actions", None, Context="Author")
    action = _field(actions, "Exec")
    _field(action, "Command", str(task.executable))
    _field(action, "Arguments", subprocess.list2cmdline(task.arguments))
    _field(action, "WorkingDirectory", str(task.executable.parent))
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n' + ET.tostring(root, encoding="unicode")
    ).encode("utf-16")


def validate_registered(xml: str, task: ComponentTask) -> tuple[bool, str]:
    """Reject extra actions, wrong user, weakened settings or altered command."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False, "component Task Scheduler XML is invalid"
    if root.tag != _tag("Task"):
        return False, "component Task Scheduler XML root is invalid"
    namespace = {"t": NAMESPACE}

    def value(path: str) -> str:
        node = root.find(path, namespace)
        return "" if node is None or node.text is None else node.text.strip()

    def sid_match(candidate: str) -> bool:
        if candidate.casefold() == task.sid.casefold():
            return True
        if sys.platform != "win32":
            return False
        from .windows_service import _identity_matches_sid

        return _identity_matches_sid(candidate, task.sid)

    actions = root.find("./t:Actions", namespace)
    triggers = root.find("./t:Triggers", namespace)
    principals = root.find("./t:Principals", namespace)
    matches = {
        "URI": value("./t:RegistrationInfo/t:URI") == task.uri,
        "SID": sid_match(value("./t:Principals/t:Principal/t:UserId")),
        "logon trigger SID": sid_match(value("./t:Triggers/t:LogonTrigger/t:UserId")),
        "logon token": value("./t:Principals/t:Principal/t:LogonType") == "InteractiveToken",
        "least privilege": value("./t:Principals/t:Principal/t:RunLevel") in ("", "LeastPrivilege"),
        "single task instance": value("./t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew",
        "non-expiring process": value("./t:Settings/t:ExecutionTimeLimit") == "PT0S",
        "repetition": value("./t:Triggers/t:TimeTrigger/t:Repetition/t:Interval") == REPETITION,
        "indefinite": not value("./t:Triggers/t:TimeTrigger/t:Repetition/t:Duration"),
        "start boundary": bool(value("./t:Triggers/t:TimeTrigger/t:StartBoundary")),
        "logon enabled": value("./t:Triggers/t:LogonTrigger/t:Enabled").lower() != "false",
        "timer enabled": value("./t:Triggers/t:TimeTrigger/t:Enabled").lower() != "false",
        "repetition continues": value(
            "./t:Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd"
        ).lower()
        in ("", "false"),
        # Power and startup policy are part of task ownership, not optional
        # implementation hints. A foreign XML edit must not wake a sleeping
        # workstation, gate availability on AC/network/idle, hide the task or
        # disable the operator's ability to stop it.
        #
        # Some Windows XML exports omit settings whose schema default already
        # has the safe value. Require explicit values where omission would be
        # unsafe (battery and catch-up policy); accept omission for the others.
        "WakeToRun": value("./t:Settings/t:WakeToRun").lower() in ("", "false", "0"),
        "DisallowStartIfOnBatteries": value("./t:Settings/t:DisallowStartIfOnBatteries").lower()
        in ("false", "0"),
        "StopIfGoingOnBatteries": value("./t:Settings/t:StopIfGoingOnBatteries").lower()
        in ("false", "0"),
        "StartWhenAvailable": value("./t:Settings/t:StartWhenAvailable").lower() in ("true", "1"),
        "RunOnlyIfNetworkAvailable": value("./t:Settings/t:RunOnlyIfNetworkAvailable").lower()
        in ("", "false", "0"),
        "RunOnlyIfIdle": value("./t:Settings/t:RunOnlyIfIdle").lower() in ("", "false", "0"),
        "Hidden": value("./t:Settings/t:Hidden").lower() in ("", "false", "0"),
        "AllowStartOnDemand": value("./t:Settings/t:AllowStartOnDemand").lower()
        in ("", "true", "1"),
        "AllowHardTerminate": value("./t:Settings/t:AllowHardTerminate").lower()
        in ("", "true", "1"),
        "restart interval": value("./t:Settings/t:RestartOnFailure/t:Interval") == RESTART_INTERVAL,
        "restart count": value("./t:Settings/t:RestartOnFailure/t:Count") == RESTART_COUNT,
        "executable": os.path.normcase(value("./t:Actions/t:Exec/t:Command"))
        == os.path.normcase(str(task.executable)),
        "arguments": value("./t:Actions/t:Exec/t:Arguments")
        == subprocess.list2cmdline(task.arguments),
        "working directory": os.path.normcase(value("./t:Actions/t:Exec/t:WorkingDirectory"))
        == os.path.normcase(str(task.executable.parent)),
        "one action": actions is not None and len(list(actions)) == 1,
        "one Exec": len(root.findall("./t:Actions/t:Exec", namespace)) == 1,
        "two triggers": triggers is not None and len(list(triggers)) == 2,
        "one principal": principals is not None and len(list(principals)) == 1,
    }
    bad = [name for name, ok in matches.items() if not ok]
    return (False, "component task contract mismatch: " + ", ".join(bad)) if bad else (True, "")

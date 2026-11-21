"""Read-only post-failure Windows I1 native state inventory.

Operates only against fixed Task Scheduler names, current-user private
*non-secret* Core manifests/process identity, and one loopback listener.
Never starts/stops services, reads HTTP keys or mutates files.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

INSTANCE = "i1-smoke"
PORT = 18868
TASK = "ServerFS v25 i1-smoke Core"
PRODUCTION = (
    ("ServerFS Native Tunnel", True, True),
    ("ServerFS v25 Core", True, True),
    ("ServerFS v25 Cloudflare Connector", True, True),
    ("ServerFS v25 Agent Bridge", False, False),
    ("ServerFS v25 OpenAI Connector", False, False),
)
_QUERY = r"""
$ErrorActionPreference='Stop'
$all=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {$_.TaskPath -eq '\'})
$names=@(
'ServerFS Native Tunnel',
'ServerFS v25 Core',
'ServerFS v25 Cloudflare Connector',
'ServerFS v25 Agent Bridge',
'ServerFS v25 OpenAI Connector',
'ServerFS v25 i1-smoke Core'
)
$rows=@(foreach($name in $names) {
 $found=@($all | Where-Object {$_.TaskName -ceq $name})
 if($found.Count -ne 1) {
  [PSCustomObject]@{name=$name;count=$found.Count;state='UNKNOWN';enabled=$false}
 } else {
  [PSCustomObject]@{
   name=$name;count=1;state=[string]$found[0].State;
   enabled=[bool]$found[0].Settings.Enabled
  }
 }
})
$ports=@(Get-NetTCPConnection -State Listen -ErrorAction Stop |
 Where-Object {$_.LocalPort -eq 18868} |
 ForEach-Object {
 [PSCustomObject]@{address=[string]$_.LocalAddress;pid=[int]$_.OwningProcess}
})
[PSCustomObject]@{tasks=$rows;listeners=$ports} | ConvertTo-Json -Depth 5 -Compress
"""


def _flag(name: str, value: object) -> None:
    print(f"I1_POSTFAIL_{name}={value}")


def parse_inventory(data: object) -> tuple[dict[str, dict], list[dict]]:
    if not isinstance(data, dict):
        raise ValueError("unexpected Scheduler inventory")
    tasks = data.get("tasks")
    ports = data.get("listeners")
    if not isinstance(tasks, list) or not isinstance(ports, list):
        raise ValueError("unexpected Scheduler/listener inventory")
    if len(tasks) != len(PRODUCTION) + 1:
        raise ValueError("Task Scheduler inventory incomplete")
    found = {}
    for row in tasks:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError("invalid Task Scheduler row")
        name = row["name"]
        if name in found:
            raise ValueError("duplicate Task Scheduler entry")
        found[name] = row
    return found, ports


def summarize_inventory(tasks: dict[str, dict], ports: list[dict]) -> None:
    baseline = True
    for name, should_run, should_enable in PRODUCTION:
        row = tasks.get(name)
        if not isinstance(row, dict) or row.get("count") != 1:
            baseline = False
            _flag("TASK_" + name.replace(" ", "_").upper(), "NOT_VERIFIED")
            continue
        running = row.get("state") == "Running"
        enabled = row.get("enabled") is True
        valid = enabled == should_enable and running == should_run
        baseline = baseline and valid
        _flag(
            "TASK_" + name.replace(" ", "_").upper(),
            "EXPECTED" if valid else "DIFFERS",
        )
    _flag("PRODUCTION_TASK_BASELINE", "PASS" if baseline else "DIFFERS")
    fixture = tasks.get(TASK)
    if fixture is None or fixture.get("count") != 1:
        _flag("FIXTURE_TASK", "NOT_VERIFIED")
    else:
        state = fixture.get("state")
        _flag(
            "FIXTURE_TASK_STATE",
            state if state in ("Disabled", "Running", "Ready", "Queued") else "UNKNOWN",
        )
        _flag("FIXTURE_TASK_ENABLED", fixture.get("enabled") is True)
    _flag("LOOPBACK_18868_LISTENERS", len(ports))
    _flag(
        "LOOPBACK_18868_ONLY",
        len(ports) == 1 and ports[0].get("address") == "127.0.0.1",
    )


def main() -> int:
    if sys.platform != "win32" or os.name != "nt":
        _flag("PLATFORM", "UNSUPPORTED")
        return 2
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _QUERY],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if result.returncode:
        _flag("SCHEDULER_QUERY", "FAILED")
        return 2
    try:
        tasks, listeners = parse_inventory(json.loads(result.stdout.decode("utf-8-sig")))
    except (ValueError, UnicodeError):
        _flag("SCHEDULER_QUERY", "MALFORMED")
        return 2
    _flag("SCHEDULER_QUERY", "PASS")
    summarize_inventory(tasks, listeners)

    # Load non-secret component manifests and live kernel process identity only.
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity

    for instance, label in (("default", "PRODUCTION"), (INSTANCE, "FIXTURE")):
        try:
            manifest = storage.load("core", instance)
            proc = _current_identity(manifest)
            _flag(label + "_CORE_MANIFEST", "VALID")
            _flag(
                label + "_CORE_PROCESS",
                "ALIVE" if proc is not None else "NOT_ALIVE_OR_NOT_VERIFIED",
            )
            if instance == INSTANCE and proc is not None:
                owned = any(
                    row.get("pid") == proc.pid and row.get("address") == "127.0.0.1"
                    for row in listeners
                )
                _flag("FIXTURE_LISTENER_PID_MATCH", owned)
        except Exception:
            _flag(label + "_CORE_MANIFEST", "UNAVAILABLE_OR_UNVERIFIED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        _flag("UNEXPECTED_ERROR", "REDACTED")
        raise SystemExit(2) from None

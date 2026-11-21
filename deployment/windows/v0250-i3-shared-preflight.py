"""Read-only Windows I3 shared-Bridge / two-Core host preflight.

Checks three fresh identities and two distinct loopback ports. No installer,
credential, task registration, state/ACL mutation or network call is made.
This is a source-only gate until run in a real non-elevated Windows session.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys

BRIDGE_ID = "i3-shared"
CORE_PORTS = {"i3-core-a": 18869, "i3-core-b": 18870}
PRODUCTION = {
    "ServerFS Native Tunnel": (True, "Running"),
    "ServerFS v25 Core": (True, "Running"),
    "ServerFS v25 Cloudflare Connector": (True, "Running"),
    "ServerFS v25 Agent Bridge": (False, None),
    "ServerFS v25 OpenAI Connector": (False, None),
}
TARGETS = (
    "ServerFS v25 i3-shared Agent Bridge",
    "ServerFS v25 i3-core-a Core",
    "ServerFS v25 i3-core-b Core",
)
SCRIPT = r"""
$ErrorActionPreference='Stop'
$names=@(
  'ServerFS Native Tunnel',
  'ServerFS v25 Core',
  'ServerFS v25 Cloudflare Connector',
  'ServerFS v25 Agent Bridge',
  'ServerFS v25 OpenAI Connector',
  'ServerFS v25 i3-shared Agent Bridge',
  'ServerFS v25 i3-core-a Core',
  'ServerFS v25 i3-core-b Core'
)
$records=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {
  $_.TaskPath -eq '\' -and $names -ccontains $_.TaskName
} | ForEach-Object {
  [PSCustomObject]@{
    name=$_.TaskName
    state=[string]$_.State
    enabled=[bool]$_.Settings.Enabled
  }
})
ConvertTo-Json -InputObject $records -Compress
"""


class I3PreflightError(ValueError):
    """Redacted Windows multi-Core preflight refusal."""


def verify_task_inventory(rows: object) -> None:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise I3PreflightError("I3 Scheduler inventory is invalid")
    try:
        names = [row["name"] for row in rows]
        if (
            any(type(name) is not str for name in names)
            or len(set(names)) != len(names)
            or len(names) != len(PRODUCTION)
            or any(name in TARGETS for name in names)
        ):
            raise I3PreflightError("I3 Scheduler identity is already occupied")
        for name, (enabled, running) in PRODUCTION.items():
            matches = [row for row in rows if row["name"] == name]
            if len(matches) != 1 or type(matches[0].get("enabled")) is not bool:
                raise I3PreflightError("I3 protected Scheduler baseline is incomplete")
            row = matches[0]
            if row["enabled"] != enabled or not isinstance(row.get("state"), str):
                raise I3PreflightError("I3 protected Scheduler baseline changed")
            if running is not None and row["state"] != running:
                raise I3PreflightError("I3 production task is not Running")
            if running is None and row["state"] != "Disabled":
                raise I3PreflightError("I3 inactive production component state is inconsistent")
    except (TypeError, KeyError, ValueError) as exc:
        raise I3PreflightError("I3 Scheduler inventory is invalid") from exc


def verify_ports_free(listeners: object) -> None:
    if not isinstance(listeners, (list, tuple)):
        raise I3PreflightError("I3 TCP listener inventory unavailable")
    try:
        if any(item.port in CORE_PORTS.values() for item in listeners):
            raise I3PreflightError("I3 private Core port is already occupied")
    except I3PreflightError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise I3PreflightError("I3 TCP listener inventory invalid") from exc


def main() -> None:
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25 import storage

    if (
        sys.platform != "win32"
        or os.name != "nt"
        or not sys.flags.isolated
        or not sys.dont_write_bytecode
        or ctypes.windll.shell32.IsUserAnAdmin()
    ):
        raise I3PreflightError("native non-elevated Windows Python -I -B required")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise I3PreflightError("I3 Scheduler query is unavailable")
    try:
        inventory = json.loads(result.stdout.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise I3PreflightError("I3 Scheduler output is invalid") from exc
    verify_task_inventory(inventory)
    print("I3_PRODUCTION_SCHEDULER=EXPECTED")
    for instance in (BRIDGE_ID, *CORE_PORTS):
        private_root = storage.root(instance)
        if private_root.exists() or private_root.is_symlink():
            raise I3PreflightError("I3 instance directory is already occupied")
    print("I3_THREE_INSTANCE_ROOTS=ABSENT")
    verify_ports_free(_windows_ipv4_tcp_listeners())
    print("I3_TWO_LOOPBACK_PORTS=UNBOUND")
    print("I3_NATIVE_PREFLIGHT=PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - fixed nonsecret marker only
        print("I3_NATIVE_PREFLIGHT=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

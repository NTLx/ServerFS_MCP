"""I4 topology-C: read-only host preflight for two independent Core/Bridge pairs.

This verifies a fresh, non-elevated Windows fixture namespace without writes.
The existing five production and five I1/I2/I3 fixture tasks are protected.
No task installation/stop/start, credentials or service files are modified.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys

BRIDGES = ("i4-a", "i4-b")
CORE_PORTS = {"i4-a": 18871, "i4-b": 18872}
PAIRS = dict(zip(CORE_PORTS, BRIDGES, strict=True))
PRODUCTION = {
    "ServerFS Native Tunnel": (True, "Running"),
    "ServerFS v25 Core": (True, "Running"),
    "ServerFS v25 Cloudflare Connector": (True, "Running"),
    "ServerFS v25 Agent Bridge": (False, "Disabled"),
    "ServerFS v25 OpenAI Connector": (False, "Disabled"),
}
EXISTING_FIXTURES = (
    "ServerFS v25 i1-smoke Core",
    "ServerFS v25 i2-smoke Agent Bridge",
    "ServerFS v25 i3-shared Agent Bridge",
    "ServerFS v25 i3-core-a Core",
    "ServerFS v25 i3-core-b Core",
)
TARGETS = (
    *(f"ServerFS v25 {name} Agent Bridge" for name in BRIDGES),
    *(f"ServerFS v25 {name} Core" for name in CORE_PORTS),
)
KNOWN = (*PRODUCTION, *EXISTING_FIXTURES, *TARGETS)
# Query only the explicit known owner task identities, never a wildcard mutation.
_QUERY = r"""
$ErrorActionPreference='Stop'
$names=@(
  'ServerFS Native Tunnel',
  'ServerFS v25 Core',
  'ServerFS v25 Cloudflare Connector',
  'ServerFS v25 Agent Bridge',
  'ServerFS v25 OpenAI Connector',
  'ServerFS v25 i1-smoke Core',
  'ServerFS v25 i2-smoke Agent Bridge',
  'ServerFS v25 i3-shared Agent Bridge',
  'ServerFS v25 i3-core-a Core',
  'ServerFS v25 i3-core-b Core',
  'ServerFS v25 i4-a Agent Bridge',
  'ServerFS v25 i4-b Agent Bridge',
  'ServerFS v25 i4-a Core',
  'ServerFS v25 i4-b Core'
)
$found=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {
  $_.TaskPath -eq '\' -and $names -ccontains $_.TaskName
} | ForEach-Object {
  [PSCustomObject]@{
    name=$_.TaskName
    state=[string]$_.State
    enabled=[bool]$_.Settings.Enabled
  }
})
ConvertTo-Json -InputObject $found -Compress
"""


class I4PreflightError(RuntimeError):
    """Do not disclose production paths, keys or task command lines."""


def verify_task_rows(rows: object, *, require_candidates_absent: bool = True) -> None:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise I4PreflightError("I4 Scheduler inventory is malformed")
    names = [row.get("name") for row in rows]
    if any(type(name) is not str for name in names) or len(set(names)) != len(names):
        raise I4PreflightError("I4 Scheduler task identity is ambiguous")
    if not set(names) <= set(KNOWN):
        raise I4PreflightError("I4 Scheduler inventory includes unexpected identity")
    for name, (enabled, state) in PRODUCTION.items():
        hits = [row for row in rows if row["name"] == name]
        if len(hits) != 1 or hits[0].get("enabled") is not enabled or hits[0].get("state") != state:
            raise I4PreflightError("I4 protected production Scheduler baseline differs")
    # Existing acceptance fixture tasks are never touched by the installer.
    for name in EXISTING_FIXTURES:
        hits = [row for row in rows if row["name"] == name]
        if (
            len(hits) != 1
            or hits[0].get("enabled") is not False
            or hits[0].get("state") != "Disabled"
        ):
            raise I4PreflightError("I4 existing acceptance fixture must be Disabled")
    if require_candidates_absent:
        if any(row["name"] in TARGETS for row in rows):
            raise I4PreflightError("I4 candidate Scheduler identity is occupied")
        if len(rows) != len(PRODUCTION) + len(EXISTING_FIXTURES):
            raise I4PreflightError("I4 protected Scheduler inventory is incomplete")
    else:
        for name in TARGETS:
            hits = [row for row in rows if row["name"] == name]
            if (
                len(hits) != 1
                or hits[0].get("enabled") is not False
                or hits[0].get("state") != "Disabled"
            ):
                raise I4PreflightError("I4 candidate Scheduler task must be Disabled")
        if len(rows) != len(KNOWN):
            raise I4PreflightError("I4 candidate Scheduler inventory is incomplete")


def scheduler_rows() -> list[dict]:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _QUERY],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=25,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise I4PreflightError("I4 Scheduler query failed")
    try:
        rows = json.loads(result.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise I4PreflightError("I4 Scheduler query returned invalid JSON") from exc
    if not isinstance(rows, list):
        raise I4PreflightError("I4 Scheduler inventory is not an array")
    return rows


def preflight() -> None:
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25 import storage

    if (
        sys.platform != "win32"
        or os.name != "nt"
        or not sys.flags.isolated
        or not sys.dont_write_bytecode
        or ctypes.windll.shell32.IsUserAnAdmin()
    ):
        raise I4PreflightError("I4 requires non-elevated native Python -I -B")
    verify_task_rows(scheduler_rows())
    print("I4_PROTECTED_SCHEDULER=EXPECTED")
    # Owned mode requires instance_id == bridge_id; each pair shares one
    # private installation root but maintains separate component services.
    if set(BRIDGES) != set(CORE_PORTS) or any(
        instance_id != bridge_id for instance_id, bridge_id in PAIRS.items()
    ):
        raise I4PreflightError("I4 pair ownership identity is invalid")
    for instance_id in BRIDGES:
        path = storage.root(instance_id)
        if path.exists() or path.is_symlink():
            raise I4PreflightError("I4 candidate private instance root is occupied")
    print("I4_TWO_PRIVATE_INSTANCE_ROOTS=ABSENT")
    listeners = _windows_ipv4_tcp_listeners()
    if any(listener.port in CORE_PORTS.values() for listener in listeners):
        raise I4PreflightError("I4 proposed loopback port is occupied")
    print("I4_TWO_LOOPBACK_PORTS=FREE")
    print("I4_NATIVE_PREFLIGHT=PASS")


if __name__ == "__main__":
    try:
        preflight()
    except Exception as exc:  # noqa: BLE001 - privacy-preserving status only
        print("I4_NATIVE_PREFLIGHT=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

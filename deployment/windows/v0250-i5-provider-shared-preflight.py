"""I5: read-only native preflight for a dedicated real-Provider Bridge fixture.

Protects all five production plus nine I1-I4 acceptance tasks; rejects any
pre-existing i5-prefixed Scheduler identity or private root. The new fixture
will have two independent Cores sharing one Provider-enabled Bridge, but
preflight does not create/enable/start any task or inspect any credential.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

BRIDGE = "i5-shared"
CORE_PORTS = {"i5-core-a": 18873, "i5-core-b": 18874}
TARGETS = (
    "ServerFS v25 i5-shared Agent Bridge",
    "ServerFS v25 i5-core-a Core",
    "ServerFS v25 i5-core-b Core",
)
# Explicit optional, separately owned Connectors may coexist with the three
# I5 Core/Bridge tasks. Unknown i5-prefixed Scheduler identities remain denied.
# This is a Scheduler inventory allowlist, not a Tunnel-token ownership grant.
CONNECTOR_TARGETS = (
    "ServerFS v25 i5-core-a Cloudflare Connector i5-edge-a",
    "ServerFS v25 i5-core-b Cloudflare Connector i5-edge-b",
    # I10: two temporary, independent, disabled-only Cloudflare owners
    # targeting the SAME installed I5 Core A. No wildcard allowance.
    "ServerFS v25 i5-core-a Cloudflare Connector i10-edge-a",
    "ServerFS v25 i5-core-a Cloudflare Connector i10-edge-b",
)
_QUERY_I5 = r"""
$ErrorActionPreference='Stop'
$found=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {
  $_.TaskPath -eq '\' -and $_.TaskName -like 'ServerFS v25 i5-*'
} | ForEach-Object {
  [PSCustomObject]@{
    name=$_.TaskName
    state=[string]$_.State
    enabled=[bool]$_.Settings.Enabled
  }
})
ConvertTo-Json -InputObject $found -Compress
"""


class I5PreflightError(RuntimeError):
    """Nonsecret native host refusal."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5PreflightError(reason)


def _i4_gate():
    path = Path(__file__).with_name("v0250-i4-independent-preflight.py")
    spec = importlib.util.spec_from_file_location("i5_protected_i4_gate", path)
    require(spec is not None and spec.loader is not None, "I5 protected source gate missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidates() -> list[dict]:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _QUERY_I5],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=25,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0, "I5 candidate Scheduler query unavailable")
    try:
        rows = json.loads(result.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise I5PreflightError("I5 candidate Scheduler output invalid") from exc
    require(isinstance(rows, list), "I5 candidate inventory is not a list")
    return rows


def verify_candidates(rows: object, *, installed: bool) -> None:
    require(
        isinstance(rows, list) and all(isinstance(item, dict) for item in rows),
        "I5 candidate inventory malformed",
    )
    names = [row.get("name") for row in rows]
    require(
        all(type(name) is str for name in names) and len(set(names)) == len(names),
        "I5 candidate identity is ambiguous",
    )
    if not installed:
        require(not rows, "I5 candidate Scheduler identity already exists")
        return
    require(
        set(TARGETS) <= set(names)
        and set(names) <= set(TARGETS) | set(CONNECTOR_TARGETS)
        and 3 <= len(rows) <= len(TARGETS) + len(CONNECTOR_TARGETS),
        "I5 exact Core/Bridge/Connector task inventory differs",
    )
    require(
        all(row.get("state") == "Disabled" and row.get("enabled") is False for row in rows),
        "I5 tasks not all Disabled",
    )


def protected_tasks() -> None:
    # I4 already knows all 5 production and all 9 earlier acceptance tasks,
    # so enforce its exact Disabled/Running baseline without duplicating it.
    gate = _i4_gate()
    require(set(gate.TARGETS).isdisjoint(TARGETS), "I5 reuses existing I4 identity")
    gate.verify_task_rows(gate.scheduler_rows(), require_candidates_absent=False)


def preflight() -> None:
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25 import storage

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I5 needs native non-elevated Windows -I -B",
    )
    protected_tasks()
    print("I5_PROTECTED_PRODUCTION_AND_I1_I4=EXPECTED")
    verify_candidates(candidates(), installed=False)
    print("I5_THREE_SCHEDULER_IDENTITIES=FREE")
    for instance in (BRIDGE, *CORE_PORTS):
        root = storage.root(instance)
        require(
            not root.exists() and not root.is_symlink(),
            "I5 new private instance root already occupied",
        )
    print("I5_THREE_PRIVATE_INSTANCE_ROOTS=ABSENT")
    listeners = _windows_ipv4_tcp_listeners()
    require(
        not any(item.port in CORE_PORTS.values() for item in listeners),
        "I5 dedicated Core port already occupied",
    )
    print("I5_CORE_PORTS_18873_18874=FREE")
    print("I5_NATIVE_PREFLIGHT=PASS")


if __name__ == "__main__":
    try:
        preflight()
    except BaseException as exc:  # noqa: BLE001 - no path, SID or secret output
        print("I5_NATIVE_PREFLIGHT=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

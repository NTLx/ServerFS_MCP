"""I11 read-only Windows preflight: two NEW independent Agent-enabled owned pairs.

Unlike I4, I11 will use two distinct native owned Bridges, each with its
own Claude Worker, private writable disposable Fixture and signed Core grant.
Never convert the accepted I4 read-only fixtures into Provider-enabled homes.

This module is strictly READ-ONLY: no Task Scheduler operations other than
enumeration, no private file writes, credentials, process starts or Agent turn.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

IDS = ("i11-a", "i11-b")
PORTS = {"i11-a": 18881, "i11-b": 18882}
TASK_NAMES = frozenset(
    f"ServerFS v25 {instance} {label}" for instance in IDS for label in ("Core", "Agent Bridge")
)
PHASE = "entry"
QUERY = r"""
$ErrorActionPreference = 'Stop'
$rows = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
  $_.TaskPath -eq '\' -and $_.TaskName -like 'ServerFS v25 i11-*'
} | ForEach-Object {
  [pscustomobject]@{ name=$_.TaskName; state=[string]$_.State; enabled=[bool]$_.Settings.Enabled }
})
ConvertTo-Json -InputObject $rows -Compress
"""


class I11PreflightRefusal(RuntimeError):
    """Failure class with no Windows private data in output."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I11PreflightRefusal(reason)


def helper(filename: str, label: str):
    src = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, src)
    require(spec is not None and spec.loader is not None, "fixed Windows preflight helper absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate_tasks() -> list[dict]:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", QUERY],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0, "I11 private task inventory query failed")
    try:
        rows = json.loads(result.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise I11PreflightRefusal("I11 task inventory malformed") from None
    require(
        isinstance(rows, list)
        and all(isinstance(row, dict) for row in rows)
        and all(isinstance(row.get("name"), str) for row in rows)
        and len({row["name"] for row in rows}) == len(rows),
        "I11 task inventory ambiguous",
    )
    return rows


def verify_tasks(rows: list[dict], *, installed: bool) -> None:
    require(isinstance(rows, list), "I11 Scheduler inventory is not a list")
    names = [row.get("name") if isinstance(row, dict) else None for row in rows]
    require(
        all(type(name) is str for name in names) and len(names) == len(set(names)),
        "I11 Scheduler identities ambiguous",
    )
    if not installed:
        require(not rows, "I11 prospective task prefix occupied")
        return
    require(
        set(names) == TASK_NAMES
        and len(rows) == 4
        and all(row.get("enabled") is False and row.get("state") == "Disabled" for row in rows),
        "I11 four owned Scheduler tasks not exactly Disabled",
    )


def native() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "I11 requires native Windows current SID and ordinary-user -I -B",
    )


def preflight() -> None:
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25 import storage

    native()
    gate = helper("v0250-i4-independent-preflight.py", "i11_exact_protected_owner_gate")
    gate.verify_task_rows(gate.scheduler_rows(), require_candidates_absent=False)
    i7 = helper("v0250-i7-connectors-disabled-register.py", "i11_original_i7_protection")
    i7.postflight()  # requires existing I1-I7, I5 and production owner protections
    print("I11_EXISTING_WINDOWS_PRODUCTION_I1_I7_PROTECTED=PASS")
    require(len(IDS) == len(PORTS) == 2 and set(IDS) == set(PORTS), "I11 instance IDs not distinct")
    require(len(TASK_NAMES) == 4, "I11 proposed Scheduler names collide")
    verify_tasks(candidate_tasks(), installed=False)
    print("I11_FOUR_CANDIDATE_TASK_IDENTITIES=FREE")
    for instance in IDS:
        root = storage.root(instance)
        require(not root.exists() and not root.is_symlink(), "I11 private root occupied")
    print("I11_TWO_PRIVATE_ROOTS=ABSENT")
    listeners = _windows_ipv4_tcp_listeners()
    require(
        not any(row.port in PORTS.values() for row in listeners),
        "I11 proposed Core port in use",
    )
    print("I11_TWO_LOOPBACK_PORTS_18881_18882=FREE")
    claude = shutil.which("claude")
    require(
        isinstance(claude, str) and Path(claude).is_file() and os.access(claude, os.X_OK),
        "I11 configured Claude CLI unavailable",
    )
    print("I11_CLAUDE_CLI_RESOLVED=PASS")
    print("I11_TWO_OWNED_PROVIDER_PAIRS_PREFLIGHT=PASS")


if __name__ == "__main__":
    try:
        require(not sys.argv[1:], "I11 preflight accepts no extra arguments")
        preflight()
    except BaseException as exc:  # noqa: BLE001 - release only the class/phase
        print(
            "I11_PREFLIGHT=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

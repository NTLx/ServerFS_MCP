"""I13 native Windows READ-ONLY preflight for two NEW owned Repo Ops pairs.

Exact names i13-a / i13-b and loopback 18883 / 18884. Before any install:
verify protected production/I1-I7 and I11 instances, private roots absent,
four Scheduler identities free, ports unbound, user Git executable available.
No model, Repo Ops command, Git modification, credentials or Connector.
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

IDS = ("i13-a", "i13-b")
PORTS = {"i13-a": 18883, "i13-b": 18884}
TASK_NAMES = frozenset(
    f"ServerFS v25 {instance} {label}" for instance in IDS for label in ("Core", "Agent Bridge")
)
# I14: exact optional, temporary Cloudflare Owners. Never accept running
# or unknown i13-prefixed tasks; initial I13 installation still requires none.
OPTIONAL_CONNECTOR_NAMES = frozenset(
    f"ServerFS v25 {instance} Cloudflare Connector i14-edge-{letter}"
    for instance, letter in (("i13-a", "a"), ("i13-b", "b"))
)
PHASE = "entry"
QUERY = r"""
$ErrorActionPreference = 'Stop'
$rows = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
  $_.TaskPath -eq '\' -and $_.TaskName -like 'ServerFS v25 i13-*'
} | ForEach-Object {
  [pscustomobject]@{
    name=$_.TaskName; state=[string]$_.State;
    enabled=[bool]$_.Settings.Enabled
  }
})
ConvertTo-Json -InputObject $rows -Compress
"""


class I13PreflightRefusal(RuntimeError):
    """Never emit private paths, grants, secrets or raw OS errors."""


def require(ok: bool, label: str) -> None:
    if not ok:
        raise I13PreflightRefusal(label)


def helper(filename: str, label: str):
    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "I13 approved helper missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def task_rows() -> list[dict]:
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", QUERY],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(proc.returncode == 0, "I13 exact task inventory query failed")
    try:
        items = json.loads(proc.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise I13PreflightRefusal("I13 task inventory malformed") from None
    require(
        isinstance(items, list)
        and all(isinstance(item, dict) and isinstance(item.get("name"), str) for item in items)
        and len({item["name"] for item in items}) == len(items),
        "I13 task inventory ambiguous",
    )
    return items


def verify_tasks(rows: list[dict], *, installed: bool) -> None:
    require(
        isinstance(rows, list)
        and all(isinstance(row, dict) and type(row.get("name")) is str for row in rows)
        and len({row["name"] for row in rows}) == len(rows),
        "I13 Scheduler task identities ambiguous",
    )
    if not installed:
        require(rows == [], "I13 fresh task namespace occupied")
        return
    require(
        {row["name"] for row in rows} in (TASK_NAMES, TASK_NAMES | OPTIONAL_CONNECTOR_NAMES)
        and all(row.get("enabled") is False and row.get("state") == "Disabled" for row in rows),
        "I13 exact Repo owners and optional Connectors must be Disabled",
    )


def native() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "I13 Windows ordinary SID -I -B required",
    )


def preflight() -> None:
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25 import storage

    native()
    old = helper("v0250-i11-two-owned-provider-stage.py", "i13_protected_i11_postflight")
    old.postflight()  # includes protected I4/I5/I6/I7 and production
    print("I13_EXISTING_I1_I11_AND_PRODUCTION=PROTECTED")
    require(len(PORTS) == len(IDS) == 2 and set(PORTS) == set(IDS), "I13 planned IDs overlap")
    require(len(TASK_NAMES) == 4, "I13 task identities collide")
    verify_tasks(task_rows(), installed=False)
    print("I13_FOUR_NEW_NATIVE_TASK_NAMES=FREE")
    for instance in IDS:
        root = storage.root(instance)
        require(not root.exists() and not root.is_symlink(), "I13 private instance root occupied")
    print("I13_TWO_PRIVATE_ROOTS=ABSENT")
    listeners = _windows_ipv4_tcp_listeners()
    require(
        not any(item.port in PORTS.values() for item in listeners),
        "I13 selected Core TCP port occupied",
    )
    print("I13_LOOPBACK_18883_18884=FREE")
    git = shutil.which("git")
    require(
        isinstance(git, str) and Path(git).is_file() and os.access(git, os.X_OK),
        "I13 user Git CLI unavailable",
    )
    print("I13_WINDOWS_GIT_CLI_AVAILABLE=PASS")
    print("I13_TWO_REPO_ONLY_OWNED_PAIRS_PREFLIGHT=PASS")


if __name__ == "__main__":
    try:
        require(not sys.argv[1:], "I13 preflight takes no arguments")
        preflight()
    except BaseException as exc:  # noqa: BLE001 - sanitized failure only
        print(
            "I13_REPO_PREFLIGHT=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

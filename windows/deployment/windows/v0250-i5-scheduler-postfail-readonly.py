"""Read-only I5 Bridge Scheduler result after one refused native startup.

Reports only nonsecret LastTaskResult, whether it ran, the authenticated
task identity status and I4/I5 runtime.log presence flags. No XML/command
lines/private files or raw error messages are printed.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_QUERY = r"""
$ErrorActionPreference='Stop'
$name='ServerFS v25 i5-shared Agent Bridge'
$info=Get-ScheduledTaskInfo -TaskPath '\' -TaskName $name -ErrorAction Stop
[PSCustomObject]@{
  result=[long]$info.LastTaskResult
  ever_run=($info.LastRunTime.Year -ge 2000)
} | ConvertTo-Json -Compress
"""


class I5SchedulerEvidenceError(RuntimeError):
    """Do not expose protected task actions or credentials."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5SchedulerEvidenceError(reason)


def _gate():
    path = Path(__file__).with_name("v0250-i5-provider-shared-preflight.py")
    spec = importlib.util.spec_from_file_location("i5_safe_scheduler_gate", path)
    require(spec is not None and spec.loader is not None, "I5 host gate missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_info(payload: bytes) -> tuple[int, bool]:
    data = json.loads(payload.decode("utf-8-sig"))
    require(
        isinstance(data, dict)
        and type(data.get("result")) is int
        and type(data.get("ever_run")) is bool,
        "I5 Scheduler run info shape invalid",
    )
    return data["result"] & 0xFFFFFFFF, data["ever_run"]


def main() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I5 requires native non-elevated Python -I -B",
    )
    gate = _gate()
    gate.protected_tasks()
    gate.verify_candidates(gate.candidates(), installed=True)
    owner = storage.load("bridge", "i5-shared")
    scheduler = Scheduler()
    require(scheduler._task_xml(owner) is not None, "I5 task owner XML failed validation")
    status = scheduler.status(owner)
    require(
        status.get("registered") is True
        and status.get("enabled") is False
        and status.get("state") == "Disabled"
        and status.get("running") is False
        and _current_identity(owner) is None,
        "I5 owner must be fully Disabled and offline",
    )
    print("I5_SCHEDULER_POSTFAIL_OWNED_DISABLED=PASS")

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _QUERY],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=25,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0, "I5 Scheduler last-result query unavailable")
    last_code, ever_run = parse_info(result.stdout)
    print("I5_SCHEDULER_LAST_RESULT=0x" + f"{last_code:08X}")
    print("I5_SCHEDULER_WAS_STARTED=" + ("YES" if ever_run else "NO"))

    sid = current_user_sid()
    for instance in ("i4-a", "i5-shared"):
        log = storage.home("bridge", instance) / "runtime.log"
        if log.is_file() and not log.is_symlink():
            storage.check_private(log, directory=False, sid=sid)
            value = "PRESENT"
        else:
            value = "ABSENT"
        print("I5_SCHEDULER_" + instance.replace("-", "_").upper() + "_RUNTIME_LOG=" + value)
    print("I5_SCHEDULER_POSTFAIL_READONLY=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - only sanitized exception class
        print("I5_SCHEDULER_POSTFAIL=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

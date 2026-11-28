"""Read-only native Win32 process-tree observations after the console fix.

PowerShell and its console-server conhost.exe processes can still exist
when CREATE_NO_WINDOW prevents visible windows. Their existence is recorded
as diagnostic counts, NEVER treated as proof of a visible popup.

A PASS means the exact Cloudflare owner remained alive throughout process
sampling; graphical visibility requires separate observation on the desktop.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import NamedTuple


class SnapshotCount(NamedTuple):
    powershell_pids: frozenset[int]
    console_host_pids: frozenset[int]


def classify_processes(
    entries,
    *,
    owner_pid: int,
    observed_powershell: frozenset[int] = frozenset(),
) -> SnapshotCount:
    """Classify exact direct children; no global conhost.exe process matches."""
    pids = {
        row.pid
        for row in entries
        if row.parent_pid == owner_pid and row.executable_name.casefold() == "powershell.exe"
    }
    remembered = pids | set(observed_powershell)
    hosts = {
        row.pid
        for row in entries
        if row.executable_name.casefold() == "conhost.exe"
        and row.parent_pid in (remembered | {owner_pid})
    }
    return SnapshotCount(frozenset(remembered), frozenset(hosts))


def sample_console_hosts(
    *,
    owner_pid: int,
    snapshots,
    monotonic=time.monotonic,
    sleep=time.sleep,
    duration_seconds: float = 12.0,
    interval_seconds: float = 0.15,
) -> SnapshotCount:
    """Observe for the full bounded interval; conhost is diagnostic only."""
    started = monotonic()
    seen_shell: frozenset[int] = frozenset()
    seen_hosts: frozenset[int] = frozenset()
    while True:
        observed = classify_processes(
            snapshots(),
            owner_pid=owner_pid,
            observed_powershell=seen_shell,
        )
        seen_shell = observed.powershell_pids
        seen_hosts |= observed.console_host_pids
        if monotonic() - started >= duration_seconds:
            return SnapshotCount(seen_shell, seen_hosts)
        sleep(interval_seconds)


STAGE = "entry"


def main() -> None:
    global STAGE
    from serverfs_mcp.windows_process import process_entries
    from serverfs_mcp.windows_v25 import readiness, storage
    from serverfs_mcp.windows_v25.tasks import Scheduler

    STAGE = "exact-owner-argument"
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise ValueError("verified Cloudflare owner PID argument is required")
    required_pid = int(sys.argv[1])
    if required_pid <= 0:
        raise ValueError("verified owner PID invalid")
    if not (
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
    ):
        raise ValueError("isolated Windows Controller required")
    expected = storage.root() / "envs" / "cloudflare"
    if os.path.normcase(str(Path(sys.prefix).resolve(strict=True))) != (
        os.path.normcase(str(expected.resolve(strict=True)))
    ):
        raise ValueError("incorrect isolated Cloudflare controller")

    STAGE = "initial-generation"
    manifest = storage.load("cloudflare")
    state = Scheduler().status(manifest)
    owner = readiness._current_identity(manifest)
    if not (
        state["running"]
        and state["enabled"]
        and state["readiness"] == "owner_alive"
        and owner is not None
        and owner.pid == required_pid
    ):
        raise ValueError("current Cloudflare owner is not verified")

    STAGE = "bounded-process-observation"
    observed = sample_console_hosts(
        owner_pid=owner.pid,
        snapshots=process_entries,
    )
    STAGE = "same-generation-confirmation"
    after = readiness._current_identity(manifest)
    state_after = Scheduler().status(manifest)
    if not (
        after is not None
        and after == owner
        and state_after["running"]
        and state_after["enabled"]
        and state_after["readiness"] == "owner_alive"
    ):
        raise ValueError("Cloudflare generation changed while sampling")
    print("V25_CF_WINDOWLESS_POWERSHELL_CHILD_PIDS=" + str(len(observed.powershell_pids)))
    print("V25_CF_WINDOWLESS_ASSOCIATED_CONHOST_PIDS=" + str(len(observed.console_host_pids)))
    # Neither PowerShell nor conhost PID existence proves a GUI popup:
    # conhost can serve headless console processes. Visual acceptance is
    # explicitly separate and should be reported by the local operator.
    STAGE = "accepted"
    print("V25_CF_WINDOWLESS_PROCESS_DIAGNOSTIC=PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V25_CF_WINDOWLESS_CONSOLE_PROBE=FAILED:" + type(exc).__name__ + " stage=" + STAGE,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

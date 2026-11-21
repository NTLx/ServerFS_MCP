"""Immutable SQLite proof of one real Claude AskUserApproval cancellation.

Runs only under installed I5 Bridge Worker private venv *after* Bridge
Task Scheduler is Disabled. Receives exact task ID on stdin (never argv),
emits exactly one fixed PASS marker. No raw Provider payload/request text,
task ID, private paths, credentials or event payloads on output.
"""

from __future__ import annotations

import ctypes
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ID = re.compile(r"agt_[0-9a-f]{24}\Z")
QUERY = r"""
$ErrorActionPreference='Stop'
$t=@(Get-ScheduledTask -TaskPath '\' -TaskName 'ServerFS v25 i5-shared Agent Bridge')
if($t.Count -ne 1 -or [string]$t[0].State -ne 'Disabled' -or $t[0].Settings.Enabled){exit 11}
[Console]::Out.Write('I5_BRIDGE_DISABLED')
"""


class I5ApprovalProofError(RuntimeError):
    pass


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5ApprovalProofError(reason)


def verify(con: sqlite3.Connection, task_id: str) -> None:
    tasks = con.execute(
        """
        SELECT t.status, t.pending_request_id, t.final_response,
               t.native_session_id
        FROM tasks t
        JOIN core_task_owners o ON o.task_id = t.task_id
        WHERE t.task_id = ? AND o.core_id = 'i5-core-a'
          AND t.runtime = 'claude' AND t.workdir_alias = 'Fixture'
        """,
        (task_id,),
    ).fetchall()
    require(len(tasks) == 1, "cancelled Core-A task not unique")
    status, pending_id, final, native_session = tasks[0]
    require(
        status == "cancelled"
        and pending_id is None
        and final is None
        and isinstance(native_session, str)
        and bool(native_session),
        "cancelled SDK-native session not durable",
    )
    requests = con.execute(
        """
        SELECT kind, status
        FROM pending_requests WHERE task_id = ?
        ORDER BY created_at
        """,
        (task_id,),
    ).fetchall()
    require(
        len(requests) == 1 and requests[0][0] == "approval" and requests[0][1] == "stale",
        "Claude approval request not exactly one stale record",
    )
    events = con.execute(
        "SELECT event_type FROM events WHERE task_id = ? ORDER BY event_id LIMIT 300",
        (task_id,),
    ).fetchall()
    require(2 <= len(events) < 300, "approval audit list incomplete")
    kinds = [row[0] for row in events]
    require(
        kinds.count("task.started") == 1
        and kinds.count("approval.requested") == 1
        and kinds.count("task.cancelled") == 1
        and not any(
            kind in kinds
            for kind in (
                "approval.answered",
                "approval.resolved",
                "task.completed",
                "command.completed",
                "file_change.started",
                "file.changed",
            )
        )
        and not any(kind.startswith("file_change.") for kind in kinds),
        "Claude approval got a late tool/response or invalid terminal event",
    )


def main() -> None:
    from serverfs_agent_bridge.private_state import verify_private_file

    require(
        not sys.argv[1:]
        and os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native installed nonadmin I5 Worker -I -B required",
    )
    local = os.environ.get("LOCALAPPDATA")
    require(isinstance(local, str) and bool(local), "Windows current-user home unavailable")
    root = Path(local) / "ServerFS/v25/instances/i5-shared"
    expected = root / "envs/agent-bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I5 Worker private Python identity invalid",
    )
    raw = sys.stdin.buffer.read(160)
    require(len(raw) == 28 and raw.isascii(), "exact bounded task identifier required")
    task_id = raw.decode("ascii")
    require(ID.fullmatch(task_id) is not None, "task identifier syntax invalid")

    task = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", QUERY],
        input=None,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(
        task.returncode == 0 and task.stdout.strip() == b"I5_BRIDGE_DISABLED",
        "I5 Bridge not safely offline",
    )

    db = root / "services/bridge/state/state.sqlite3"
    require(
        db.is_file() and not db.is_symlink() and 0 < db.stat().st_size < 100 * 1024 * 1024,
        "I5 Bridge private database invalid",
    )
    verify_private_file(
        db,
        not_regular="state database not regular",
        not_private="state file private ACL invalid",
    )
    require(not db.with_name(db.name + "-wal").exists(), "state WAL uncheckpointed")
    con = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=4)
    try:
        con.execute("PRAGMA query_only=ON")
        verify(con, task_id)
    finally:
        con.close()
    print("I5_REAL_APPROVAL_PRIVATE_STALE_SQLITE=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - class only
        print("I5_REAL_APPROVAL_PROOF=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

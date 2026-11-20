"""Read-only real Claude text-emission cancellation proof under the installed I5 Worker.

Invoked only by the I5 one-shot parent after three fixture services have
stopped. Task ID arrives on stdin, never argv, environment or output.
The worker owns the serverfs_agent_bridge import and its Windows ACL policy;
the parent Core/Bridge owner Python deliberately does not import that package.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

_ALLOWED_TASK_ID = re.compile(r"agt_[0-9a-f]{24}\Z")
_DISABLED_TASK_QUERY = r"""
$ErrorActionPreference='Stop'
$t=@(Get-ScheduledTask -TaskPath '\' -TaskName 'ServerFS v25 i5-shared Agent Bridge')
if($t.Count -ne 1 -or [string]$t[0].State -ne 'Disabled' -or $t[0].Settings.Enabled){exit 11}
[Console]::Out.Write('I5_BRIDGE_DISABLED')
"""


class I5PrivateProofError(RuntimeError):
    """Never include task IDs, paths, content or provider errors in output."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise I5PrivateProofError(message)


def verify_cancelled_task(con: sqlite3.Connection, task_id: str) -> None:
    """Check one exact task against durable owner, SDK init and event evidence."""
    rows = con.execute(
        """
        SELECT t.status, t.native_session_id, t.final_response
        FROM tasks AS t
        JOIN core_task_owners AS owner ON owner.task_id = t.task_id
        WHERE t.task_id = ? AND owner.core_id = ?
          AND t.workdir_alias = ? AND t.runtime = ?
        """,
        (task_id, "i5-core-a", "Fixture", "claude"),
    ).fetchall()
    require(len(rows) == 1, "task-not-uniquely-owned")
    task_status, session_id, final_response = rows[0]
    require(
        task_status == "cancelled"
        and isinstance(session_id, str)
        and bool(session_id)
        and final_response is None,
        "provider-init-cancel-durability-unproven",
    )
    events = con.execute(
        "SELECT event_type FROM events WHERE task_id = ? ORDER BY event_id LIMIT 500",
        (task_id,),
    ).fetchall()
    require(
        2 <= len(events) < 500 and all(isinstance(row[0], str) for row in events),
        "task-event-audit-incomplete",
    )
    kinds = [row[0] for row in events]
    require(
        "task.started" in kinds
        and "task.cancelled" in kinds
        and "task.completed" not in kinds
        and not any(event.startswith(("tool.", "command.", "file_change.")) for event in kinds),
        "cancel-task-event-audit-failed",
    )
    deltas = con.execute(
        """
        SELECT payload_json FROM events
        WHERE task_id = ? AND event_type = 'agent.text_delta'
        ORDER BY event_id LIMIT 2
        """,
        (task_id,),
    ).fetchall()
    require(len(deltas) == 1, "real-text-emission-not-uniquely-recorded")
    payload = json.loads(deltas[0][0])
    require(
        isinstance(payload, dict)
        and set(payload) == {"index", "size_bytes"}
        and type(payload["index"]) is int
        and payload["index"] >= 0
        and type(payload["size_bytes"]) is int
        and payload["size_bytes"] > 0,
        "text-delta-proof-is-not-content-free",
    )
    pending = con.execute(
        "SELECT count(*) FROM pending_requests WHERE task_id = ? AND status = ?",
        (task_id, "pending"),
    ).fetchone()
    require(pending is not None and pending[0] == 0, "pending-request-remains")


def main() -> None:
    from serverfs_agent_bridge.private_state import verify_private_file

    require(
        sys.argv[1:] == []
        and sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "installed-native-bridge-worker-required",
    )
    local = os.environ.get("LOCALAPPDATA", "")
    require(bool(local), "windows-user-home-unavailable")
    root = Path(local) / "ServerFS" / "v25" / "instances" / "i5-shared"
    expected = root / "envs" / "agent-bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "bridge-worker-interpreter-mismatch",
    )

    raw = sys.stdin.buffer.read(160)
    require(len(raw) == 28 and raw.isascii(), "task-id-shape-invalid")
    task_id = raw.decode("ascii")
    require(_ALLOWED_TASK_ID.fullmatch(task_id) is not None, "task-id-invalid")

    status = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _DISABLED_TASK_QUERY],
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(
        status.returncode == 0 and status.stdout.strip() == b"I5_BRIDGE_DISABLED",
        "bridge-not-disabled",
    )
    db = root / "services" / "bridge" / "state" / "state.sqlite3"
    require(
        db.is_file() and not db.is_symlink() and 0 < db.stat().st_size < 100 * 1024 * 1024,
        "bridge-state-db-invalid",
    )
    verify_private_file(
        db,
        not_regular="bridge-state-not-regular",
        not_private="bridge-state-private-acl-invalid",
    )
    require(
        not db.with_name(db.name + "-wal").exists(),
        "bridge-state-wal-not-checkpointed",
    )

    con = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=4)
    try:
        con.execute("PRAGMA query_only=ON")
        verify_cancelled_task(con, task_id)
    finally:
        con.close()

    print("I5_STREAM_PRIVATE_SDK_TEXT_AND_DURABLE_OWNER=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - stable exception class only
        print("I5_CANCEL_PRIVATE_PROOF=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

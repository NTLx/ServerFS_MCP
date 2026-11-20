"""Read-only I5 genuine token-stream cancellation evidence classification.

Reports only non-sensitive event counts/ordering from the newest Core A Claude
task in the installed private Bridge SQLite. No prompts, task ids, text,
credentials, payloads, model calls, Scheduler changes or database writes.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


class I5StreamDiagnosticError(RuntimeError):
    """Fixed nonsecret refusal."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5StreamDiagnosticError(reason)


def main() -> None:
    from serverfs_agent_bridge.private_state import verify_private_file

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "installed native ordinary-user Worker -I -B required",
    )
    root = Path(os.environ["LOCALAPPDATA"]) / "ServerFS/v25/instances/i5-shared"
    worker = root / "envs/agent-bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == worker.resolve(strict=True),
        "installed I5 Worker interpreter mismatch",
    )
    source = Path(__file__).with_name("v0250-i5-cancel-state-proof-worker.py")
    spec = importlib.util.spec_from_file_location("i5_stream_existing_private_proof", source)
    require(spec is not None and spec.loader is not None, "existing private proof absent")
    previous = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(previous)

    status = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            previous._DISABLED_TASK_QUERY,
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(
        status.returncode == 0 and status.stdout.strip() == b"I5_BRIDGE_DISABLED",
        "I5 Bridge fixture not fully Disabled",
    )
    db = root / "services/bridge/state/state.sqlite3"
    require(
        db.is_file()
        and not db.is_symlink()
        and 0 < db.stat().st_size < 100 * 1024 * 1024
        and not db.with_name(db.name + "-wal").exists(),
        "private SQLite file unavailable or WAL active",
    )
    verify_private_file(
        db,
        not_regular="Bridge SQLite is not a regular file",
        not_private="Bridge SQLite ACL invalid",
    )
    print("I5_STREAM_DIAG_INSTALLED_PRIVATE_READONLY=PASS")

    con = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=4)
    try:
        con.execute("PRAGMA query_only=ON")
        candidates = con.execute(
            """
            SELECT task.task_id, task.status
            FROM tasks AS task
            JOIN core_task_owners AS own ON own.task_id = task.task_id
            WHERE own.core_id = 'i5-core-a'
              AND task.runtime = 'claude' AND task.workdir_alias = 'Fixture'
            ORDER BY task.created_at DESC, task.task_id DESC LIMIT 1
            """
        ).fetchall()
        require(len(candidates) == 1, "no uniquely selected latest Core A task")
        task_id, state = candidates[0]
        require(state == "cancelled", "latest Core A task is not cancelled")
        previous.verify_cancelled_task(con, task_id)
        print("I5_STREAM_DIAG_LATEST_CORE_A_CANCEL_OWNERSHIP=PASS")

        rows = con.execute(
            """
            SELECT event_id, event_type
            FROM events WHERE task_id = ?
              AND event_type IN ('agent.text_delta','task.cancelled')
            ORDER BY event_id
            """,
            (task_id,),
        ).fetchall()
        deltas = [event_id for event_id, kind in rows if kind == "agent.text_delta"]
        cancels = [event_id for event_id, kind in rows if kind == "task.cancelled"]
        require(deltas and len(cancels) == 1, "no unique delta/cancel evidence")
        before = sum(1 for number in deltas if number < cancels[0])
        after = sum(1 for number in deltas if number > cancels[0])
        require(before > 0, "no genuine delta persisted before cancellation")
        print("I5_STREAM_DIAG_DURABLE_TEXT_BEFORE_CANCEL=PASS")
        print("I5_STREAM_DIAG_DELTA_TOTAL=" + str(len(deltas)))
        print("I5_STREAM_DIAG_DELTA_BEFORE_CANCEL=" + str(before))
        print("I5_STREAM_DIAG_DELTA_AFTER_CANCEL=" + str(after))
        window = rows[:500]
        cap_reached = len(rows) > 500
        cancel_visible = any(kind == "task.cancelled" for _id, kind in window)
        print("I5_STREAM_DIAG_FIRST500_TRUNCATED=" + ("YES" if cap_reached else "NO"))
        print("I5_STREAM_DIAG_FIRST500_CONTAINS_CANCEL=" + ("YES" if cancel_visible else "NO"))
        print("I5_STREAM_DIAG_LATE_DELTA=" + ("YES" if after else "NO"))
        print("I5_STREAM_DIAG_ALL_EVENTS_IMMUTABLE_READONLY=PASS")
    finally:
        con.close()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - fixed class only
        print("I5_STREAM_DIAG=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

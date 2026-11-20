"""I5 real Claude task-history inspector in the *installed Bridge Worker* venv.

Unlike Core manifest DACLs, Agent Bridge SQLite state files are intentionally
single-user ACLs. Validate with the Bridge's own private_state security seam,
then query a read-only immutable SQLite snapshot; no task IDs, prompt, result
text, credentials, event payload or provider error messages leave this tool.

May miss rows in an uncheckpointed WAL: report visibility/WAL facts only.
Never run TaskStore(), connect the Provider, mutate the database or service.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

SAFE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")
QUERY = r"""
$ErrorActionPreference='Stop'
$t=@(Get-ScheduledTask -TaskPath '\' -TaskName 'ServerFS v25 i5-shared Agent Bridge')
if($t.Count -ne 1 -or [string]$t[0].State -ne 'Disabled' -or $t[0].Settings.Enabled){exit 11}
[Console]::Out.Write('I5_BRIDGE_DISABLED')
"""


class I5WorkerHistoryError(RuntimeError):
    """Always redact native private state details."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5WorkerHistoryError(reason)


def safe_token(value: object) -> str:
    return value if isinstance(value, str) and SAFE.fullmatch(value) else "REDACTED"


def event_summary(events: list[str]) -> str:
    counts = Counter(safe_token(event) for event in events)
    return ",".join(f"{key}:{counts[key]}" for key in sorted(counts)) or "NONE"


def final_shape(value: object) -> str:
    """Classify only the fixed canary sentinel, never disclose provider prose."""
    if value is None:
        return "ABSENT"
    if not isinstance(value, str):
        return "INVALID"
    normalized = value.strip()
    if normalized == "I5_REAL_AGENT_OK":
        return "EXACT"
    if normalized.strip("\"'`") == "I5_REAL_AGENT_OK":
        return "QUOTED"
    if normalized.startswith("```") and normalized.endswith("```"):
        return "FENCED_WITH_SENTINEL" if "I5_REAL_AGENT_OK" in normalized else "FENCED_OTHER"
    return "CONTAINS_WITH_EXTRAS" if "I5_REAL_AGENT_OK" in normalized else "NO_SENTINEL"


def message_result_relation(final: object, message: object) -> str:
    """Categorical correlation; never return either raw Provider text."""
    if not isinstance(final, str) or not final.strip():
        return "NO_FINAL"
    if message is None:
        return "NO_MESSAGE"
    if not isinstance(message, str):
        return "INVALID_MESSAGE"
    if final == message:
        return "EXACT"
    if final.strip() == message.strip():
        return "WHITESPACE_ONLY"
    if final.strip() in message:
        return "FINAL_WITHIN_MESSAGE"
    if message.strip() and message.strip() in final:
        return "MESSAGE_WITHIN_FINAL"
    return "DIFFERENT"


def main() -> None:
    from serverfs_agent_bridge import private_state

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I5 Bridge worker must run native unelevated -I -B",
    )
    local = os.environ.get("LOCALAPPDATA", "")
    require(bool(local), "I5 current-user AppData unavailable")
    root = Path(local) / "ServerFS" / "v25" / "instances" / "i5-shared"
    expected = root / "envs" / "agent-bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I5 installed Bridge Worker interpreter required",
    )
    print("I5_HISTORY_INSTALLED_WORKER=VERIFIED")

    check = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", QUERY],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(
        check.returncode == 0 and check.stdout.strip() == b"I5_BRIDGE_DISABLED",
        "I5 Bridge task is not Disabled",
    )
    print("I5_HISTORY_SCHEDULER_BRIDGE=DISABLED")

    db = root / "services" / "bridge" / "state" / "state.sqlite3"
    require(
        db.is_file() and not db.is_symlink() and 0 < db.stat().st_size < 100 * 1024 * 1024,
        "I5 Bridge state database is not a bounded regular file",
    )
    private_state.verify_private_file(
        db,
        not_regular="Bridge-owned SQLite file is not regular",
        not_private="Bridge-owned SQLite DACL does not match the worker",
    )
    print("I5_HISTORY_BRIDGE_OWNED_SQLITE_DACL=PASS")

    wal = db.with_name(db.name + "-wal")
    print("I5_HISTORY_SQLITE_WAL=" + ("PRESENT" if wal.exists() else "ABSENT"))
    con = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=4.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        tasks = con.execute(
            """
            SELECT t.task_id, t.status, t.error_code, t.final_response,
                   t.native_session_id
            FROM tasks AS t
            JOIN core_task_owners AS owner ON owner.task_id = t.task_id
            WHERE owner.core_id = ? AND t.workdir_alias = ? AND t.runtime = ?
            ORDER BY t.created_at DESC, t.task_id DESC LIMIT 4
            """,
            ("i5-core-a", "Fixture", "claude"),
        ).fetchall()
        print("I5_HISTORY_VISIBLE_CORE_A_TASKS=" + str(len(tasks)))
        for i, record in enumerate(tasks, 1):
            mark = "I5_HISTORY_TASK_" + str(i) + "_"
            require(
                record["status"]
                in (
                    "queued",
                    "starting",
                    "running",
                    "waiting_for_approval",
                    "waiting_for_question",
                    "succeeded",
                    "failed",
                    "cancelled",
                    "interrupted",
                ),
                "I5 task status malformed",
            )
            print(mark + "STATUS=" + record["status"].upper())
            print(
                mark
                + "ERROR_CODE="
                + (safe_token(record["error_code"]) if record["error_code"] else "NONE")
            )
            final = record["final_response"]
            print(
                mark
                + "FINAL_SENTINEL="
                + (
                    "MATCH"
                    if final_shape(final) == "EXACT"
                    else "ABSENT"
                    if final is None
                    else "OTHER"
                )
            )
            print(mark + "FINAL_SHAPE=" + final_shape(final))
            print(
                mark + "NATIVE_SESSION=" + ("PRESENT" if record["native_session_id"] else "ABSENT")
            )
            event_count = con.execute(
                "SELECT count(*) FROM events WHERE task_id=?", (record["task_id"],)
            ).fetchone()
            require(
                type(event_count[0]) is int and event_count[0] <= 250,
                "I5 task event count exceeds inspectable bound",
            )
            events = con.execute(
                "SELECT event_type, payload_json FROM events "
                "WHERE task_id=? ORDER BY event_id LIMIT 250",
                (record["task_id"],),
            ).fetchall()
            event_types = [row["event_type"] for row in events]
            print(mark + "EVENT_TYPES=" + event_summary(event_types))
            messages = []
            for row in events:
                if row["event_type"] != "agent.message":
                    continue
                payload = row["payload_json"]
                require(
                    isinstance(payload, str) and len(payload) <= 131_072,
                    "I5 provider event payload exceeds safe bound",
                )
                decoded = json.loads(payload)
                require(isinstance(decoded, dict), "I5 provider event malformed")
                messages.append(decoded.get("text"))
            latest_message = messages[-1] if messages else None
            print(mark + "LAST_MESSAGE_SHAPE=" + final_shape(latest_message))
            print(
                mark + "RESULT_MESSAGE_RELATION=" + message_result_relation(final, latest_message)
            )
            pending = con.execute(
                "SELECT count(*) FROM pending_requests WHERE task_id=? AND status='pending'",
                (record["task_id"],),
            ).fetchone()
            pending_count = int(pending[0])
            print(mark + "PENDING_REQUESTS=" + str(pending_count))
            if record["status"] == "succeeded":
                complete = (
                    record["error_code"] is None
                    and isinstance(final, str)
                    and bool(final.strip())
                    and isinstance(record["native_session_id"], str)
                    and bool(record["native_session_id"])
                    and event_types.count("task.started") == 1
                    and event_types.count("task.completed") == 1
                    and len(messages) >= 1
                    and all(isinstance(message, str) for message in messages)
                    and pending_count == 0
                    and not any(
                        kind.startswith(("tool.", "command.", "file_change."))
                        or kind in {"file.changed", "task.approval.requested"}
                        for kind in event_types
                    )
                )
                print(mark + "SECURE_TERMINAL_EVIDENCE=" + ("PASS" if complete else "FAILED"))
    finally:
        con.close()
    print("I5_HISTORY_DB_OPEN=IMMUTABLE_READONLY")
    print("I5_REAL_PROVIDER_HISTORY_WORKER=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - sanitize all storage failures
        print("I5_HISTORY_WORKER=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

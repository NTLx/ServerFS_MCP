"""Read-only, redacted I5 real-Claude task history from stopped private Bridge.

No TaskStore initialization (which can migrate/write), no raw task IDs, prompt,
final response, events payloads, credentials, native session IDs or error text.
SQLite connection is *immutable mode=ro*, so the database and sidecars cannot
be changed by this inspection. A stale uncheckpointed WAL can make such a
snapshot incomplete; report count and do not infer any omitted rows.
"""

from __future__ import annotations

import ctypes
import os
import re
import sqlite3
import sys
from collections import Counter

_ALLOWED = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")


class I5HistoryError(RuntimeError):
    """Never expose private database content in diagnostic output."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5HistoryError(reason)


def safe_token(value: object) -> str:
    return value if isinstance(value, str) and _ALLOWED.fullmatch(value) else "REDACTED"


def summarize_events(values: list[str]) -> str:
    counts = Counter(safe_token(value) for value in values)
    return ",".join(f"{key}:{counts[key]}" for key in sorted(counts)) or "NONE"


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
        "native non-elevated Windows Python -I -B required",
    )
    manifest = storage.load("bridge", "i5-shared")
    status = Scheduler().status(manifest)
    require(
        status.get("registered") is True
        and status.get("enabled") is False
        and status.get("state") == "Disabled"
        and status.get("running") is False
        and status.get("readiness") == "offline"
        and _current_identity(manifest) is None,
        "I5 history requires the Bridge to be fully offline",
    )
    print("I5_HISTORY_BRIDGE_DISABLED=VERIFIED")

    sid = current_user_sid()
    db = storage.home("bridge", "i5-shared") / "state" / "state.sqlite3"
    storage.check_private(db, directory=False, sid=sid)
    require(db.is_file() and db.stat().st_size < 100 * 1024 * 1024, "I5 private DB unavailable")

    # SQLite immutable URI guarantees zero writes to the protected source DB.
    # The view may miss not-yet-checkpointed WAL rows; this is not a durability
    # validation and must not be used to delete or alter production data.
    conn = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=4)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            """
            SELECT t.task_id, t.status, t.error_code, t.final_response,
                   t.native_session_id, t.created_at
            FROM tasks AS t
            JOIN core_task_owners AS owner ON owner.task_id = t.task_id
            WHERE owner.core_id = ? AND t.workdir_alias = ? AND t.runtime = ?
            ORDER BY t.created_at DESC, t.task_id DESC LIMIT 4
            """,
            ("i5-core-a", "Fixture", "claude"),
        ).fetchall()
        print("I5_HISTORY_VISIBLE_OWNED_TASKS=" + str(len(rows)))
        for number, task in enumerate(rows, 1):
            prefix = "I5_HISTORY_TASK_" + str(number) + "_"
            status = task["status"]
            require(
                status
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
                "I5 private task status malformed",
            )
            print(prefix + "STATUS=" + status.upper())
            print(
                prefix
                + "ERROR_CODE="
                + (safe_token(task["error_code"]) if task["error_code"] is not None else "NONE")
            )
            final = task["final_response"]
            print(
                prefix
                + "FINAL_SENTINEL="
                + (
                    "NONE"
                    if final is None
                    else "MATCH"
                    if isinstance(final, str) and final.strip() == "I5_REAL_AGENT_OK"
                    else "OTHER"
                )
            )
            print(prefix + "HAS_NATIVE_SESSION=" + ("YES" if task["native_session_id"] else "NO"))
            events = conn.execute(
                """
                SELECT event_type FROM events
                WHERE task_id = ? ORDER BY event_id LIMIT 250
                """,
                (task["task_id"],),
            ).fetchall()
            print(prefix + "EVENT_TYPES=" + summarize_events([r["event_type"] for r in events]))
            requests = conn.execute(
                "SELECT count(*) FROM pending_requests WHERE task_id = ? AND status = ?",
                (task["task_id"], "pending"),
            ).fetchone()
            print(prefix + "PENDING_REQUESTS=" + str(int(requests[0])))
    finally:
        conn.close()
    print("I5_HISTORY_SQLITE_SOURCE=IMMUTABLE_READONLY")
    print("I5_REAL_PROVIDER_HISTORY_READONLY=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - never output raw DB exception text
        print("I5_REAL_PROVIDER_HISTORY=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

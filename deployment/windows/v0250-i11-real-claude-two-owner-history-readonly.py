"""I11 two independent owned Claude Workers: sanitized immutable post-failure audit.

Run independently in each installed i11-a/i11-b Agent Bridge WORKER venv.
Never invokes TaskStore (would migrate), Provider, HTTP, Scheduler lifecycle,
file mutations, or opens credentials. Reads a stopped Bridge's single native
SQLite database with immutable+query_only, and outputs only fixed enums/counts.
No task IDs, model text, event payloads, SDK errors or private paths printed.
If a WAL exists the immutable view could be stale: refuse rather than infer.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

IDS = ("i11-a", "i11-b")
SENTINELS = {"i11-a": "I11_OWNED_REAL_A_OK", "i11-b": "I11_OWNED_REAL_B_OK"}
STATUS = frozenset(
    {
        "queued",
        "starting",
        "running",
        "waiting_for_approval",
        "waiting_for_question",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
    }
)
CODES = frozenset(
    {
        "AGENT_PROVIDER_ERROR",
        "AGENT_PROVIDER_DISCONNECTED",
        "AGENT_TASK_TIMED_OUT",
        "AGENT_INTERACTION_TIMED_OUT",
        "AGENT_PROVIDER_INACTIVE",
        "BRIDGE_SHUTDOWN",
        "BRIDGE_SCHEDULING_FAILED",
    }
)
# Only printable identifier-like event names may appear in diagnostics.
# Future versions can add event types without turning this read-only
# investigation into an unrelated schema-rejection failure.
SAFE_EVENT = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
PHASE = "entry"


class I11HistoryRefusal(RuntimeError):
    """No private state leaves the host on failure."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I11HistoryRefusal(reason)


def classify_final(value, sentinel: str) -> str:
    if value is None:
        return "ABSENT"
    if not isinstance(value, str):
        return "INVALID"
    value = value.strip()
    if value == sentinel:
        return "EXACT"
    if value.strip("\"'`") == sentinel:
        return "QUOTED"
    if value.startswith("```") and value.endswith("```") and sentinel in value:
        return "FENCED"
    if sentinel in value:
        return "CONTAINS_EXTRAS"
    return "OTHER"


def foreign_sentinel_shape(value: object, *, instance: str) -> str:
    """Check cross-owner sentinel only; never emit or return model prose."""
    require(instance in IDS, "I11 foreign comparison requires known instance")
    foreign = SENTINELS[IDS[1] if instance == IDS[0] else IDS[0]]
    if value is None:
        return "ABSENT"
    if not isinstance(value, str):
        return "INVALID"
    normalized = value.strip()
    if normalized == foreign:
        return "EXACT_FOREIGN"
    if foreign in normalized:
        return "CONTAINS_FOREIGN"
    return "NO_FOREIGN"


def classify_expected_variation(value: object, sentinel: str) -> str:
    """Classify bounded canary fidelity without printing any model text."""
    if value is None:
        return "ABSENT"
    if not isinstance(value, str):
        return "INVALID"
    trimmed = value.strip()
    if trimmed == sentinel:
        return "EXACT"
    if trimmed.casefold() == sentinel.casefold():
        return "CASE_ONLY"
    wrappers = " \"'\x60.,!?;:()[]{}。！？，；："
    if trimmed.strip(wrappers) == sentinel:
        return "WRAPPER_PUNCTUATION"
    compact = "".join(trimmed.split())
    if compact.casefold() == sentinel.casefold():
        return "WHITESPACE_AND_CASE"
    if sentinel.casefold() in trimmed.casefold():
        return "CONTAINS_CASE_INSENSITIVE"
    if not trimmed:
        return "EMPTY"
    if len(trimmed) <= 128:
        return "OTHER_SHORT"
    return "OTHER_LONG"


def classify_relation(final, message) -> str:
    if not isinstance(final, str) or not final.strip():
        return "NO_FINAL"
    if message is None:
        return "NO_MESSAGE"
    if not isinstance(message, str):
        return "INVALID_MESSAGE"
    if final == message:
        return "EXACT"
    if final.strip() == message.strip():
        return "WHITESPACE"
    if final.strip() in message:
        return "FINAL_WITHIN_MESSAGE"
    if message.strip() and message.strip() in final:
        return "MESSAGE_WITHIN_FINAL"
    return "DIFFERENT"


def exact_disabled(instance: str) -> None:
    require(instance in IDS, "unknown I11 read-only owner")
    query = (
        "$ErrorActionPreference='Stop';"
        "$rows=@(Get-ScheduledTask -TaskPath '\\' "
        f"-TaskName 'ServerFS v25 {instance} Agent Bridge' -ErrorAction Stop);"
        "if($rows.Count -ne 1 -or [string]$rows[0].State -ne 'Disabled'"
        " -or $rows[0].Settings.Enabled){exit 11};"
        "[Console]::Out.Write('I11_BRIDGE_DISABLED')"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(
        result.returncode == 0 and result.stdout.strip() == b"I11_BRIDGE_DISABLED",
        "I11 private Bridge must be Disabled before history audit",
    )


def main() -> None:
    global PHASE
    from serverfs_agent_bridge.private_state import verify_private_file

    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", choices=IDS, required=True)
    args = parser.parse_args()
    instance = args.instance
    prefix = "I11_" + instance.upper().replace("-", "_") + "_HISTORY_"

    PHASE = "native-installed-worker-identity"
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "Windows native ordinary-user -I -B is required",
    )
    local = os.environ.get("LOCALAPPDATA")
    require(bool(local), "Windows user appdata unavailable")
    root = Path(local) / "ServerFS/v25/instances" / instance
    expected = root / "envs/agent-bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I11 exact installed private Worker interpreter required",
    )
    PHASE = "read-only-Bridge-disabled-and-private-SQLite"
    exact_disabled(instance)
    db = root / "services/bridge/state/state.sqlite3"
    require(
        db.is_file() and not db.is_symlink() and 0 < db.stat().st_size < 100 * 1024 * 1024,
        "I11 bounded private Worker database absent",
    )
    verify_private_file(
        db,
        not_regular="I11 Worker SQLite must be a regular file",
        not_private="I11 private Worker SQLite DACL mismatch",
    )
    wal = db.with_name(db.name + "-wal")
    require(not wal.exists(), "I11 immutable SQLite snapshot has active WAL")
    print(prefix + "BRIDGE=DISABLED")
    print(prefix + "PRIVATE_SQLITE=VERIFIED")

    PHASE = "immutable-query-only-state-and-event-categories"
    connection = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=4)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            """
            SELECT t.task_id, t.status, t.error_code, t.final_response,
                   t.native_session_id
            FROM tasks AS t
            INNER JOIN core_task_owners AS own ON own.task_id=t.task_id
            WHERE own.core_id=? AND t.runtime='claude' AND t.workdir_alias='Fixture'
            ORDER BY t.created_at DESC,t.task_id DESC LIMIT 2
            """,
            (instance,),
        ).fetchall()
        print(prefix + "VISIBLE_TASKS=" + str(len(rows)))
        require(len(rows) == 1, "I11 expected single first real task for this owned pair")
        row = rows[0]
        status = row["status"]
        require(status in STATUS, "I11 real task status unknown")
        print(prefix + "STATUS=" + status.upper())
        code = row["error_code"]
        print(
            prefix
            + "ERROR_CODE="
            + ("NONE" if code is None else code if code in CODES else "OTHER_REDACTED")
        )
        final = row["final_response"]
        print(prefix + "FINAL_SHAPE=" + classify_final(final, SENTINELS[instance]))
        print(
            prefix
            + "OWN_SENTINEL_VARIATION="
            + classify_expected_variation(final, SENTINELS[instance])
        )
        print(prefix + "FOREIGN_SENTINEL_SHAPE=" + foreign_sentinel_shape(final, instance=instance))
        print(prefix + "HAS_NATIVE_SESSION=" + ("YES" if row["native_session_id"] else "NO"))
        events = connection.execute(
            "SELECT event_type,payload_json FROM events "
            "WHERE task_id=? ORDER BY event_id LIMIT 251",
            (row["task_id"],),
        ).fetchall()
        require(len(events) <= 250, "I11 Provider event history exceeds safe bound")
        types = [
            event["event_type"]
            if isinstance(event["event_type"], str) and SAFE_EVENT.fullmatch(event["event_type"])
            else "REDACTED"
            for event in events
        ]
        counts = Counter(types)
        summary = ",".join(str(kind) + ":" + str(counts[kind]) for kind in sorted(counts)) or "NONE"
        print(prefix + "EVENT_TYPES=" + summary)
        messages = []
        for event in events:
            if event["event_type"] != "agent.message":
                continue
            raw = event["payload_json"]
            require(
                isinstance(raw, str) and len(raw) < 131072,
                "I11 Provider message too large to classify",
            )
            document = json.loads(raw)
            require(isinstance(document, dict), "I11 event payload schema invalid")
            messages.append(document.get("text"))
        latest = messages[-1] if messages else None
        print(prefix + "LATEST_MESSAGE_SHAPE=" + classify_final(latest, SENTINELS[instance]))
        print(
            prefix
            + "LATEST_MESSAGE_VARIATION="
            + classify_expected_variation(latest, SENTINELS[instance])
        )
        print(
            prefix
            + "LATEST_MESSAGE_FOREIGN_SHAPE="
            + foreign_sentinel_shape(latest, instance=instance)
        )
        print(prefix + "FINAL_MESSAGE_RELATION=" + classify_relation(final, latest))
        pending = connection.execute(
            "SELECT count(*) FROM pending_requests WHERE task_id=? AND status='pending'",
            (row["task_id"],),
        ).fetchone()
        require(type(pending[0]) is int, "I11 pending count not integer")
        print(prefix + "PENDING_REQUESTS=" + str(pending[0]))
    finally:
        connection.close()
    print(prefix + "IMMUTABLE_QUERY_ONLY=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - stable class/phase, never raw error
        print(
            "I11_REAL_PROVIDER_HISTORY=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

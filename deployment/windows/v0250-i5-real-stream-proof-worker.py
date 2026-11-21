"""Independent I5 installed-Worker proof of real streamed text before cancel.

Accepts one exact task id over stdin; never prints Provider text, prompt,
task id, credentials or DB content. Read-only immutable SQLite and installed
private ACL only after the I5 Bridge Task Scheduler owner is Disabled.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

_ID = re.compile(r"agt_[0-9a-f]{24}\Z")


class I5PrivateStreamError(RuntimeError):
    """Fixed-class refusal; no private evidence is echoed."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I5PrivateStreamError(message)


def main() -> None:
    from serverfs_agent_bridge.private_state import verify_private_file

    require(
        sys.argv[1:] == []
        and sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "installed ordinary-user Worker -I -B required",
    )
    root = Path(os.environ["LOCALAPPDATA"]) / "ServerFS/v25/instances/i5-shared"
    expected = root / "envs/agent-bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "not the installed I5 Worker interpreter",
    )
    raw = sys.stdin.buffer.read(160)
    require(
        len(raw) == 28 and raw.isascii() and _ID.fullmatch(raw.decode("ascii")) is not None,
        "invalid private task identifier",
    )
    task_id = raw.decode("ascii")

    source = Path(__file__).with_name("v0250-i5-cancel-state-proof-worker.py")
    spec = importlib.util.spec_from_file_location("i5_prior_cancel_proof", source)
    require(spec is not None and spec.loader is not None, "existing proof helper absent")
    prior = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prior)

    state = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", prior._DISABLED_TASK_QUERY],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(
        state.returncode == 0 and state.stdout.strip() == b"I5_BRIDGE_DISABLED",
        "I5 bridge not completely Disabled",
    )
    db = root / "services/bridge/state/state.sqlite3"
    require(
        db.is_file()
        and not db.is_symlink()
        and 0 < db.stat().st_size < 100 * 1024 * 1024
        and not db.with_name(db.name + "-wal").exists(),
        "private database is unavailable or uncheckpointed",
    )
    verify_private_file(
        db,
        not_regular="private SQLite is not regular",
        not_private="private SQLite ACL invalid",
    )
    con = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=4)
    try:
        con.execute("PRAGMA query_only=ON")
        prior.verify_cancelled_task(con, task_id)
        rows = con.execute(
            """
            SELECT event_id, event_type, payload_json
            FROM events
            WHERE task_id = ? AND event_type IN ('agent.text_delta', 'task.cancelled')
            ORDER BY event_id LIMIT 500
            """,
            (task_id,),
        ).fetchall()
        deltas: list[int] = []
        cancels: list[int] = []
        for event_id, event_type, payload in rows:
            if event_type == "task.cancelled":
                cancels.append(event_id)
                continue
            data = json.loads(payload)
            require(
                isinstance(data, dict)
                and type(data.get("index")) is int
                and data["index"] >= 0
                and isinstance(data.get("text"), str)
                and bool(data["text"]),
                "stream evidence payload malformed",
            )
            deltas.append(event_id)
        require(
            1 <= len(deltas) < 500 and len(cancels) == 1 and max(deltas) < cancels[0],
            "actual streamed text was not persisted before cancellation",
        )
    finally:
        con.close()
    print("I5_REAL_STREAM_PRIVATE_TEXT_BEFORE_CANCEL=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - redacted failure type only
        print("I5_PRIVATE_STREAM_PROOF=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

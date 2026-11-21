"""I5 offline cancellation proof contracts using disposable in-memory SQLite.

No installed Windows services, network, Claude Provider, task/credential files,
native ACLs or external processes are accessed by these tests.
"""

from __future__ import annotations

import functools
import importlib.util
import sqlite3
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_WORKER = _ROOT / "deployment/windows/v0250-i5-cancel-state-proof-worker.py"
_PARENT = _ROOT / "deployment/windows/v0250-i5-real-claude-inflight-cancel-smoke.py"
_TASK_ID = "agt_" + "a" * 24


@functools.lru_cache(maxsize=1)
def _module():
    spec = importlib.util.spec_from_file_location("i5_cancel_private_proof_test", _WORKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _private_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE tasks(
            task_id TEXT, status TEXT, native_session_id TEXT,
            final_response TEXT, workdir_alias TEXT, runtime TEXT
        );
        CREATE TABLE core_task_owners(task_id TEXT, core_id TEXT);
        CREATE TABLE events(task_id TEXT, event_id INTEGER, event_type TEXT);
        CREATE TABLE pending_requests(task_id TEXT, status TEXT);
        """
    )
    db.execute(
        "INSERT INTO tasks VALUES(?, ?, ?, ?, ?, ?)",
        (_TASK_ID, "cancelled", "native-session", None, "Fixture", "claude"),
    )
    db.execute("INSERT INTO core_task_owners VALUES(?, ?)", (_TASK_ID, "i5-core-a"))
    db.executemany(
        "INSERT INTO events VALUES(?, ?, ?)",
        [
            (_TASK_ID, 1, "task.started"),
            (_TASK_ID, 2, "task.cancelled"),
        ],
    )
    db.commit()
    return db


def test_i5_private_proof_accepts_exact_native_session_owned_cancel() -> None:
    db = _private_db()
    try:
        _module().verify_cancelled_task(db, _TASK_ID)
        assert db.execute("SELECT status FROM tasks").fetchone() == ("cancelled",)
    finally:
        db.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "no-session",
        "finished",
        "final-response",
        "foreign-owner",
        "wrong-workdir",
        "wrong-runtime",
        "missing-start",
        "missing-cancel",
        "completed-after-cancel",
        "tool-call",
        "command-call",
        "file-change",
        "pending-approval",
        "duplicate-ownership",
    ],
)
def test_i5_private_proof_fails_closed_on_bad_evidence(mutation: str) -> None:
    db = _private_db()
    try:
        if mutation == "no-session":
            db.execute("UPDATE tasks SET native_session_id=NULL")
        elif mutation == "finished":
            db.execute("UPDATE tasks SET status='succeeded'")
        elif mutation == "final-response":
            db.execute("UPDATE tasks SET final_response='completed text'")
        elif mutation == "foreign-owner":
            db.execute("UPDATE core_task_owners SET core_id='i5-core-b'")
        elif mutation == "wrong-workdir":
            db.execute("UPDATE tasks SET workdir_alias='Other'")
        elif mutation == "wrong-runtime":
            db.execute("UPDATE tasks SET runtime='fake'")
        elif mutation == "missing-start":
            db.execute("DELETE FROM events WHERE event_type='task.started'")
        elif mutation == "missing-cancel":
            db.execute("DELETE FROM events WHERE event_type='task.cancelled'")
        elif mutation == "completed-after-cancel":
            db.execute("INSERT INTO events VALUES(?, ?, ?)", (_TASK_ID, 3, "task.completed"))
        elif mutation == "tool-call":
            db.execute("INSERT INTO events VALUES(?, ?, ?)", (_TASK_ID, 3, "tool.started"))
        elif mutation == "command-call":
            db.execute("INSERT INTO events VALUES(?, ?, ?)", (_TASK_ID, 3, "command.started"))
        elif mutation == "file-change":
            db.execute("INSERT INTO events VALUES(?, ?, ?)", (_TASK_ID, 3, "file_change.started"))
        elif mutation == "pending-approval":
            db.execute("INSERT INTO pending_requests VALUES(?, 'pending')", (_TASK_ID,))
        elif mutation == "duplicate-ownership":
            db.execute("INSERT INTO core_task_owners VALUES(?, 'i5-core-a')", (_TASK_ID,))
        db.commit()
        with pytest.raises(_module().I5PrivateProofError):
            _module().verify_cancelled_task(db, _TASK_ID)
    finally:
        db.close()


def test_i5_private_proof_uses_installed_worker_private_db_and_readonly() -> None:
    worker = _WORKER.read_text(encoding="utf-8")
    parent = _PARENT.read_text(encoding="utf-8")
    for fragment in (
        "from serverfs_agent_bridge.private_state import verify_private_file",
        "sys.argv[1:] == []",
        "sys.stdin.buffer.read(160)",
        '"agent-bridge" / "Scripts" / "python.exe"',
        '"services" / "bridge" / "state" / "state.sqlite3"',
        '"?mode=ro&immutable=1"',
        'con.execute("PRAGMA query_only=ON")',
        '"I5_BRIDGE_DISABLED"',
        '"I5_CANCEL_PRIVATE_CLAUDE_INIT_AND_DURABLE_OWNER=PASS"',
    ):
        assert fragment in worker
    for fragment in (
        '"v0250-i5-cancel-state-proof-worker.py"',
        'input=task_id.encode("ascii")',
        "capture_output=True",
        "response.stdout.strip() == marker",
        "and not response.stderr",
        '"-I", "-B", str(proof_script)',
    ):
        assert fragment in parent
    assert "from serverfs_agent_bridge" not in parent
    assert "print(task_id)" not in worker
    assert "print(task_id)" not in parent

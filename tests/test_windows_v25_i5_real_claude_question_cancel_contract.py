"""Pure contracts for one authentic Claude SDK AskUserQuestion pending-cancel gate.

Source-only pytest; it never calls a real Provider or starts Scheduler jobs.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SMOKE = REPO / "deployment/windows/v0250-i5-real-claude-question-cancel-smoke.py"
PROOF = REPO / "deployment/windows/v0250-i5-real-question-proof-worker.py"


def load(path: Path, label: str):
    spec = importlib.util.spec_from_file_location(label, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = load(SMOKE, "i5_real_question_smoke_test")
proof = load(PROOF, "i5_real_question_proof_test")


def test_one_real_provider_question_uses_only_installed_I5_private_runtime() -> None:
    assert smoke.BRIDGE == "i5-shared"
    assert smoke.CORES == {"i5-core-a": 18873, "i5-core-b": 18874}
    source = SMOKE.read_text(encoding="utf-8")
    for expected in (
        '["--run-once"]',
        '"AskUserQuestion"',
        '"waiting_for_question"',
        '"question.requested"',
        '"task.question.answer"',
        '"CORE_TASK_NOT_FOUND"',
        '"REQUEST_STALE"',
        'smoke.cli("bridge", BRIDGE, "start")',
        'smoke.cli("core", core, "start")',
        'smoke.cli(component, instance, "stop")',
        "smoke.protected_snapshot(gate) == baseline",
        "now.creation_time == generation.creation_time",
        "from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding",
        "I5_REAL_PROVIDER_PENDING_QUESTION_CANCEL_E2E=PASS",
    ):
        assert expected in source
    for forbidden in (
        "approve_once(",
        "approve_session(",
        'session.call_tool("answer_agent_question"',
        'session.call_tool("run_repo_action"',
        'session.call_tool("submit_agent_task", {"runtime": "codex"',
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "cloudflare.request(",
        "tunnel.token",
        "git push",
    ):
        assert forbidden not in source


@pytest.mark.parametrize(
    "kind,payload,allowed",
    [
        ("task.started", {}, True),
        ("question.requested", {"request_id": "redacted"}, True),
        ("tool.started", {"tool": "AskUserQuestion"}, True),
        ("tool.started", {"tool": "Read"}, False),
        ("tool.started", {"tool": "Bash"}, False),
        ("command.started", {"tool": "Bash"}, False),
        ("file_change.started", {}, False),
        ("task.completed", {}, False),
        ("approval.resolved", {}, False),
    ],
)
def test_event_filter_never_accepts_non_question_provider_tools(
    kind: str, payload: dict, allowed: bool
) -> None:
    entry = {"event_type": kind, "payload": payload}
    if allowed:
        assert smoke.safe_events([entry]) == {kind}
    else:
        with pytest.raises(smoke.I5RealQuestionError):
            smoke.safe_events([entry])


def _conn(
    *,
    task_status: str = "cancelled",
    pending_status: str = "stale",
    owner: str = "i5-core-a",
    include_completed: bool = False,
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT, status TEXT, pending_request_id TEXT,
            final_response TEXT, native_session_id TEXT,
            runtime TEXT, workdir_alias TEXT
        );
        CREATE TABLE core_task_owners (task_id TEXT, core_id TEXT);
        CREATE TABLE pending_requests (task_id TEXT, kind TEXT, status TEXT, created_at TEXT);
        CREATE TABLE events (task_id TEXT, event_id INTEGER, event_type TEXT);
        """
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
        ("agt_test", task_status, None, None, "sdk-session", "claude", "Fixture"),
    )
    conn.execute("INSERT INTO core_task_owners VALUES (?,?)", ("agt_test", owner))
    conn.execute(
        "INSERT INTO pending_requests VALUES (?,?,?,?)",
        ("agt_test", "question", pending_status, "1"),
    )
    for i, kind in enumerate(
        ("task.started", "question.requested", "task.cancelled")
        + (("task.completed",) if include_completed else ()),
        1,
    ):
        conn.execute("INSERT INTO events VALUES (?,?,?)", ("agt_test", i, kind))
    return conn


def test_worker_reads_private_SQLite_and_proves_stale_request_without_raw_text() -> None:
    con = _conn()
    try:
        proof.verify(con, "agt_test")
    finally:
        con.close()


@pytest.mark.parametrize(
    "task_status,pending_status,owner,include_completed",
    [
        ("succeeded", "stale", "i5-core-a", False),
        ("cancelled", "pending", "i5-core-a", False),
        ("cancelled", "stale", "i5-core-b", False),
        ("cancelled", "stale", "i5-core-a", True),
    ],
)
def test_worker_fails_closed_on_owner_state_request_and_completion(
    task_status: str, pending_status: str, owner: str, include_completed: bool
) -> None:
    con = _conn(
        task_status=task_status,
        pending_status=pending_status,
        owner=owner,
        include_completed=include_completed,
    )
    try:
        with pytest.raises(proof.I5QuestionProofError):
            proof.verify(con, "agt_test")
    finally:
        con.close()


def test_worker_proof_does_not_disclose_private_sqlite_or_Provider_data() -> None:
    text = PROOF.read_text(encoding="utf-8")
    for expected in (
        "verify_private_file(",
        'db.as_uri() + "?mode=ro&immutable=1"',
        'con.execute("PRAGMA query_only=ON")',
        "len(requests) == 1",
        'requests[0][1] == "stale"',
        'kinds.count("question.requested") == 1',
        "I5_REAL_QUESTION_PRIVATE_STALE_SQLITE=PASS",
    ):
        assert expected in text
    for forbidden in (
        "TaskStore(",
        "CREATE TABLE",
        "INSERT INTO",
        "DELETE FROM",
        "UPDATE tasks",
        "Scheduler().start(",
        "print(task_id)",
        "print(session_id)",
        "print(final_response)",
        "ANTHROPIC_API_KEY",
        "cloudflare.request(",
    ):
        assert forbidden not in text

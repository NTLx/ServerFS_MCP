"""Bridge worker's own Windows private-state ACL + immutable DB probe contracts.

These tests are source-only and do not access installed Bridge credentials,
private state, Task Scheduler or real Provider API.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-provider-state-worker-readonly.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_bridge_state_worker_gate", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_worker_state_diagnostic_sanitizes_all_error_tokens() -> None:
    m = _module()
    assert m.safe_token("AGENT_PROVIDER_ERROR") == "AGENT_PROVIDER_ERROR"
    assert m.safe_token("PRIVATE_SECRET_HERE") == "PRIVATE_SECRET_HERE"
    assert m.safe_token("prefix=value/path") == "REDACTED"
    assert m.safe_token(None) == "REDACTED"
    assert m.event_summary(["task.started", "task.failed"]) == ("task.failed:1,task.started:1")
    assert m.event_summary([]) == "NONE"
    with pytest.raises(m.I5WorkerHistoryError):
        m.require(False, "test rejection")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, "ABSENT"),
        ("I5_REAL_AGENT_OK", "EXACT"),
        ('"I5_REAL_AGENT_OK"', "QUOTED"),
        ("```\nI5_REAL_AGENT_OK\n```", "FENCED_WITH_SENTINEL"),
        ("Result: I5_REAL_AGENT_OK.", "CONTAINS_WITH_EXTRAS"),
        ("different model response", "NO_SENTINEL"),
    ],
)
def test_provider_response_classification_never_returns_raw_text(
    payload: str | None, expected: str
) -> None:
    mod = _module()
    assert mod.final_shape(payload) == expected


@pytest.mark.parametrize(
    ("final", "message", "expected"),
    [
        (None, "message", "NO_FINAL"),
        ("result", None, "NO_MESSAGE"),
        ("result", {"text": "bad"}, "INVALID_MESSAGE"),
        ("same", "same", "EXACT"),
        ("same ", " same", "WHITESPACE_ONLY"),
        ("short", "the short answer", "FINAL_WITHIN_MESSAGE"),
        ("the short answer", "short", "MESSAGE_WITHIN_FINAL"),
        ("first", "second", "DIFFERENT"),
    ],
)
def test_worker_classifies_provider_message_without_disclosing_text(
    final: object, message: object, expected: str
) -> None:
    mod = _module()
    assert mod.message_result_relation(final, message) == expected


def test_bridge_worker_uses_provider_state_own_acl_and_immutable_connection() -> None:
    source = _SOURCE.read_text("utf-8")
    for required in (
        "from serverfs_agent_bridge import private_state",
        "private_state.verify_private_file(",
        "I5_HISTORY_BRIDGE_OWNED_SQLITE_DACL=PASS",
        "Get-ScheduledTask",
        "I5_HISTORY_SCHEDULER_BRIDGE=DISABLED",
        'db.as_uri() + "?mode=ro&immutable=1"',
        'con.execute("PRAGMA query_only=ON")',
        "JOIN core_task_owners",
        '("i5-core-a", "Fixture", "claude")',
        "I5_HISTORY_SQLITE_WAL=",
        "I5_REAL_PROVIDER_HISTORY_WORKER=PASS",
        "RESULT_MESSAGE_RELATION=",
        "SECURE_TERMINAL_EVIDENCE=",
        "SELECT event_type, payload_json FROM events",
        "type(event_count[0]) is int and event_count[0] <= 250",
    ):
        assert required in source
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "TaskStore")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "TaskStore")
        )
        for node in ast.walk(tree)
    )
    for forbidden in (
        "from serverfs_mcp.windows_v25 import storage",
        "CREATE TABLE",
        "DELETE FROM",
        "INSERT INTO",
        "UPDATE tasks",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "storage.prepare(",
        "storage.stage(",
        ".write_text(",
        ".write_bytes(",
        "print(record[",
        "print(events)",
        "print(final)",
        "print(db)",
        "secret_hex",
        "ANTHROPIC_API_KEY",
        "git push",
    ):
        assert forbidden not in source

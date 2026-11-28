"""I5 immutable SQLite task-history inspection: pure source/utility tests.

Does not open any installed task store or execute SQLite on host.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-real-provider-history-readonly.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_provider_history_gate", _SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_i5_history_never_discloses_raw_model_text_or_error_detail() -> None:
    mod = _module()
    assert mod.safe_token("AGENT_PROVIDER_ERROR") == "AGENT_PROVIDER_ERROR"
    assert mod.safe_token("secret=do-not-print") == "REDACTED"
    assert mod.safe_token("C:\\Users\\secret") == "REDACTED"
    assert mod.safe_token(None) == "REDACTED"
    with pytest.raises(mod.I5HistoryError):
        mod.require(False, "invalid")


def test_i5_history_only_summarizes_normalized_event_types() -> None:
    mod = _module()
    assert (
        mod.summarize_events(["task.started", "agent.message", "agent.message", "tool.started"])
        == "agent.message:2,task.started:1,tool.started:1"
    )
    assert mod.summarize_events(["unsafe\ncredential"]) == "REDACTED:1"
    assert mod.summarize_events([]) == "NONE"


def test_i5_history_uses_immutable_ro_connection_and_selected_nonsecret_columns() -> None:
    source = _SOURCE.read_text("utf-8")
    for required in (
        'status.get("state") == "Disabled"',
        "_current_identity(manifest) is None",
        "storage.check_private(db, directory=False, sid=sid)",
        '"?mode=ro&immutable=1"',
        'conn.execute("PRAGMA query_only=ON")',
        "JOIN core_task_owners",
        '("i5-core-a", "Fixture", "claude")',
        "I5_HISTORY_VISIBLE_OWNED_TASKS=",
        "I5_HISTORY_SQLITE_SOURCE=IMMUTABLE_READONLY",
        "I5_REAL_PROVIDER_HISTORY_READONLY=PASS",
    ):
        assert required in source
    for forbidden in (
        "TaskStore(",
        "CREATE TABLE",
        "INSERT INTO",
        "DELETE FROM",
        "UPDATE tasks",
        "sqlite3.connect(db,",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "storage.stage(",
        "storage.prepare(",
        ".write_bytes(",
        ".write_text(",
        "print(task[",
        "print(events)",
        "print(final)",
        "git push",
        "secret_hex",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in source

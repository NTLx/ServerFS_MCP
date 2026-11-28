"""I5 protected Task Scheduler drift: source-only read-only contracts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "deployment" / "windows"
_SCRIPT = _ROOT / "v0250-i5-protected-task-diff-readonly.py"


def _module():
    spec = importlib.util.spec_from_file_location("i5_task_diff", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gate():
    return _module().load_i4_gate()


def _valid_rows(gate):
    return [
        {"name": name, "state": state, "enabled": enabled}
        for name, (enabled, state) in gate.PRODUCTION.items()
    ] + [
        {"name": name, "state": "Disabled", "enabled": False}
        for name in (*gate.EXISTING_FIXTURES, *gate.TARGETS)
    ]


def test_i5_14_protected_task_baseline_matches_exactly() -> None:
    m, gate = _module(), _gate()
    rows = _valid_rows(gate)
    assert len(rows) == 14
    assert m.mismatches(gate, rows) == []


@pytest.mark.parametrize("case", ["stopped", "disabled", "fixture_enabled", "missing", "duplicate"])
def test_i5_protected_task_mismatch_visible_without_mutation(case: str) -> None:
    m, gate = _module(), _gate()
    rows = _valid_rows(gate)
    if case == "stopped":
        rows[0]["state"] = "Ready"
    elif case == "disabled":
        rows[0]["enabled"] = False
    elif case == "fixture_enabled":
        rows[-1]["enabled"] = True
    elif case == "missing":
        rows.pop()
    elif case == "duplicate":
        rows.append(dict(rows[-1]))
    assert m.mismatches(gate, rows)


def test_i5_protected_task_diff_uses_proven_query_and_never_writes() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "gate.scheduler_rows()" in source
    assert "gate.verify_task_rows(rows, require_candidates_absent=False)" in source
    assert "I5_PROTECTED_DIFF=" in source
    for forbidden in (
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Scheduler().",
        "storage.prepare(",
        "storage.stage(",
        ".write_bytes(",
        ".write_text(",
        "git push",
        "secret_hex",
    ):
        assert forbidden not in source

"""Source-only contract for privacy-preserving I5 postfailure stream diagnosis."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i5-real-stream-postfail-readonly.py"
)
_SPEC = importlib.util.spec_from_file_location("i5_stream_diagnostic_contract", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
diagnostic = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(diagnostic)


def test_diagnostic_is_private_readonly_bounded_and_no_model() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for required in (
        "verify_private_file(",
        "previous.verify_cancelled_task(con, task_id)",
        "mode=ro&immutable=1",
        "PRAGMA query_only=ON",
        "I5_STREAM_DIAG_DURABLE_TEXT_BEFORE_CANCEL=PASS",
        "I5_STREAM_DIAG_FIRST500_TRUNCATED=",
        "I5_STREAM_DIAG_LATE_DELTA=",
        "len(rows) > 500",
    ):
        assert required in source
    for forbidden in (
        "TaskStore(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "httpx2.AsyncClient",
        "submit_agent_task",
        "git push",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in source


def test_diagnostic_refuses_invalid_observation() -> None:
    with pytest.raises(diagnostic.I5StreamDiagnosticError):
        diagnostic.require(False, "not accepted")
    diagnostic.require(True, "accepted")

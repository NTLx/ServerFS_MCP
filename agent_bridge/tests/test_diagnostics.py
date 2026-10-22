"""Contract and hard-bound tests for the internal diagnostics recorder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_agent_bridge import diagnostics
from serverfs_agent_bridge.diagnostics import DiagnosticOperation, DiagnosticRecorder
from serverfs_agent_bridge.observability import RECOVERY_OUTCOMES


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def operation(kind: str = "agent.execution", duration: float = 0.1, **kwargs: object):
    sequence = kwargs.pop("sequence", 0)
    return DiagnosticOperation(kind=kind, duration_seconds=duration, sequence=sequence, **kwargs)


def recorder() -> tuple[DiagnosticRecorder, Clock]:
    clock = Clock()
    return DiagnosticRecorder(clock=clock), clock


def test_bridge_generated_context_and_idempotent_task_slot() -> None:
    recorder_, _ = recorder()
    assert recorder_.admit_task("task-a", "acp")
    context = recorder_._slots["task-a"].context
    assert len(context.trace_id) == 32
    assert len(context.root_span_id) == 16
    assert not recorder_.admit_task("task-a", "codex")
    assert recorder_._slots["task-a"].context is context
    assert recorder_.snapshot()["retained_task_slot_count"] == 1


def test_recovery_operation_reuses_existing_bounded_outcomes() -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "benchmarks/fixtures/v022_diagnostic_catalog_v1.json"
        ).read_text()
    )
    assert set(fixture["recovery_outcomes"]) == RECOVERY_OUTCOMES
    for outcome in RECOVERY_OUTCOMES:
        assert (
            DiagnosticOperation(
                kind="agent.recovery",
                duration_seconds=0.1,
                sequence=0,
                outcome=outcome,
                reason_code="ok" if outcome == "success" else "probe_failed",
            ).outcome
            == outcome
        )


def test_staged_submission_context_and_clock_attach_after_task_creation() -> None:
    rec, clock = recorder()
    staged = rec.stage_submission()
    clock.value += 2.0  # lease/task creation work before the slot has a task ID
    assert rec.admit_task("new-task", "acp", staging=staged)
    assert rec._slots["new-task"].context is staged.context
    clock.value += 3.0
    assert rec.seal_task("new-task", "succeeded")
    assert rec.task_projection("new-task")["terminal_seal"]["wall_clock_seconds"] == 5.0


def test_exact_terminal_seal_and_duplicate_seal_rejected() -> None:
    recorder_, clock = recorder()
    assert recorder_.admit_task("t", "codex")
    assert recorder_.record("t", operation())
    clock.value += 0.25
    assert recorder_.seal_task("t", "succeeded")
    assert not recorder_.seal_task("t", "failed")
    result = recorder_.task_projection("t")
    assert result["terminal_seal"]["outcome"] == "succeeded"
    assert result["terminal_seal"]["reason_code"] == "ok"
    assert result["terminal_seal"]["wall_clock_seconds"] == pytest.approx(0.25)
    assert recorder_.retained_record_count == 2


def test_prefix_truncation_keeps_contiguous_prefix_and_terminal_seal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnostics, "MAX_TASK_DIAGNOSTIC_RECORDS", 3)
    recorder_, _ = recorder()
    assert recorder_.admit_task("t", "unknown")
    assert recorder_.record("t", operation(sequence=0))
    slot = recorder_._slots["t"]
    assert recorder_.record("t", operation(sequence=1))
    assert not recorder_.record("t", operation(sequence=2))
    assert len(slot.operations) == 2
    assert recorder_.seal_task("t", "interrupted")
    assert recorder_.task_projection("t")["completeness"] == "truncated_prefix"


def test_limits_reserved_seal_and_active_slots_are_not_evicted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnostics, "MAX_DIAGNOSTIC_RECORDS", 3)
    rec, _ = recorder()
    assert rec.admit_task("active", "acp")
    assert rec.admit_task("active-2", "acp")
    assert rec.record("active", operation())
    assert rec.seal_task("active-2", "succeeded")
    assert rec.admit_task("new", "acp")  # evicts the whole oldest terminal task
    assert rec.task_projection("active-2")["reason"] == "evicted"
    assert rec.task_projection("active")["disposition"] == "available"
    assert rec.task_projection("new")["disposition"] == "available"
    assert rec.retained_record_count + rec._reserved_records <= 3
    assert rec.seal_task("active", "succeeded")
    assert rec.seal_task("new", "failed")
    assert rec.retained_record_count <= 3


def test_eviction_uses_terminal_completion_time_not_admission_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnostics, "MAX_DIAGNOSTIC_RECORDS", 3)
    rec, _ = recorder()
    assert rec.admit_task("admitted-first", "acp")
    assert rec.admit_task("admitted-second", "acp")
    assert rec.seal_task("admitted-first", "succeeded", completed_at=300.0)
    assert rec.seal_task("admitted-second", "succeeded", completed_at=200.0)
    assert rec.admit_task("active", "acp")
    assert rec.record("active", operation())
    assert rec.task_projection("admitted-second")["reason"] == "evicted"
    assert rec.task_projection("admitted-first")["disposition"] == "available"
    assert not rec.admit_task("admitted-second", "acp")
    assert len(rec._terminal_heap) == sum(slot.terminal for slot in rec._slots.values())


def test_active_tasks_cannot_be_evicted_and_admission_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnostics, "MAX_DIAGNOSTIC_RECORDS", 1)
    rec, _ = recorder()
    assert rec.admit_task("one", "acp")
    assert not rec.admit_task("two", "acp")
    assert rec.task_projection("two")["reason"] == "capacity_exhausted"
    assert rec.snapshot()["admission_failures"] == 1
    assert rec.task_projection("one")["disposition"] == "available"


def test_global_full_evicts_whole_oldest_terminal_but_never_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnostics, "MAX_DIAGNOSTIC_RECORDS", 3)
    rec, _ = recorder()
    assert rec.admit_task("terminal-old", "acp")
    assert rec.admit_task("terminal-new", "acp")
    assert rec.admit_task("active", "acp")
    assert rec.seal_task("terminal-old", "succeeded", completed_at=1.0)
    assert rec.seal_task("terminal-new", "succeeded", completed_at=2.0)
    assert rec.record("active", operation())  # record pressure evicts one whole terminal slot
    assert rec.task_projection("terminal-old")["reason"] == "evicted"
    assert rec.task_projection("terminal-new")["disposition"] == "available"
    assert rec.task_projection("active")["disposition"] == "available"
    assert rec.admit_task("active-2", "acp")  # evicts the remaining terminal slot
    assert rec.task_projection("terminal-new")["reason"] == "evicted"
    assert rec.task_projection("active")["disposition"] == "available"
    assert rec.task_projection("active-2")["disposition"] == "available"
    assert rec.snapshot()["completed_task_evictions"] == 2


def test_mass_active_admissions_cannot_exceed_global_reserved_limits() -> None:
    rec, _ = recorder()
    admitted = 0
    attempts = diagnostics.MAX_DIAGNOSTIC_RECORDS + 20
    for index in range(attempts):
        admitted += rec.admit_task(f"active-{index}", "acp")
        assert (
            rec.retained_record_count + rec._reserved_records <= diagnostics.MAX_DIAGNOSTIC_RECORDS
        )
        assert rec.retained_bytes + rec._reserved_bytes <= diagnostics.MAX_DIAGNOSTIC_BYTES
        assert len(rec._slots) <= diagnostics.MAX_DIAGNOSTIC_RECORDS
    assert admitted == diagnostics.MAX_DIAGNOSTIC_RECORDS
    assert rec.snapshot()["admission_failures"] == attempts - admitted


def test_reserved_and_operation_bytes_are_enforced_before_admission() -> None:
    assert diagnostics.MAX_DIAGNOSTIC_RECORDS == 2_048
    assert diagnostics.MAX_DIAGNOSTIC_BYTES == 1_048_576
    assert diagnostics.MAX_TASK_DIAGNOSTIC_RECORDS == 64
    assert diagnostics.MAX_TASK_DIAGNOSTIC_BYTES == 32_768
    assert diagnostics.MAX_DIAGNOSTIC_OPERATION_BYTES == 1_024
    assert diagnostics.TERMINAL_SEAL_RESERVED_BYTES == 512

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(diagnostics, "MAX_DIAGNOSTIC_BYTES", 700)
        rec, _ = recorder()
        assert rec.admit_task("active", "acp")
        assert rec.retained_bytes + rec._reserved_bytes == 512
        assert rec.record("active", operation(sequence=0))
        assert rec.retained_bytes + rec._reserved_bytes <= 700
        assert not rec.admit_task("another-active", "acp")
        assert rec.snapshot()["admission_failures"] == 1

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(diagnostics, "MAX_TASK_DIAGNOSTIC_BYTES", 600)
        rec, _ = recorder()
        assert rec.admit_task("task", "acp")
        assert rec.record("task", operation(sequence=0))
        assert not rec.record("task", operation(sequence=1))
        assert len(rec._slots["task"].operations) == 1
        assert rec._slots["task"].truncated
        assert rec.seal_task("task", "succeeded")


def test_oversized_operation_rejected_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "MAX_DIAGNOSTIC_OPERATION_BYTES", 10)
    rec, _ = recorder()
    assert rec.admit_task("t", "acp")
    assert not rec.record("t", operation(sequence=0))
    assert rec._slots["t"].operations == []
    assert rec.snapshot()["oversized_operation_rejections"] == 1
    assert rec.seal_task("t", "cancelled")


def test_multisegment_rollups_busy_excluded_and_negative_unaccounted() -> None:
    rec, clock = recorder()
    assert rec.admit_task("t", "acp")
    assert rec.record("t", operation("agent.execution", 2.0, sequence=0))
    assert rec.record(
        "t",
        operation("agent.waiting_external", 3.0, sequence=1, interaction_class="approval"),
    )
    assert rec.record("t", operation("agent.execution", 4.0, sequence=2))
    assert rec.record("t", operation("agent.lease_acquire", 7.0, sequence=3, outcome="busy"))
    assert rec.record("t", operation("agent.lease_acquire", 5.0, sequence=4, outcome="acquired"))
    clock.value += 1.0
    assert rec.seal_task("t", "succeeded")
    rollups = rec.task_projection("t")["rollups"]
    assert rollups["accounted_seconds"] == 21.0
    assert rollups["active_execution_seconds"] == 6.0
    assert rollups["external_wait_seconds"] == 3.0
    assert rollups["acquired_lease_wait_seconds"] == 5.0
    assert rollups["unaccounted_seconds"] == -20.0


def test_sequence_gaps_and_duplicates_are_rejected_without_holes() -> None:
    rec, _ = recorder()
    assert rec.admit_task("t", "acp")
    assert not rec.record("t", operation(sequence=1))
    assert rec._slots["t"].operations == []
    assert rec._slots["t"].truncated
    assert rec.seal_task("t", "failed")

    rec, _ = recorder()
    assert rec.admit_task("t", "acp")
    assert rec.record("t", operation(sequence=0))
    assert not rec.record("t", operation(sequence=0))
    assert [item.sequence for item in rec._slots["t"].operations] == [0]


def test_restarted_completeness_and_restart_not_retained() -> None:
    rec, _ = recorder()
    not_retained = rec.task_projection("old")
    assert not_retained["disposition"] == "not_retained"
    assert "reason" not in not_retained
    assert rec.task_projection("unknown", durable_exists=False)["disposition"] == "unknown_task"
    assert rec.admit_task("old", "claude", restarted=True)
    assert rec.task_projection("old")["completeness"] == "restarted"
    rec._truncate(rec._slots["old"])
    assert rec.task_projection("old")["completeness"] == "restarted_truncated_prefix"


def test_failure_is_isolated_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, _ = recorder()
    assert rec.admit_task("t", "acp")
    monkeypatch.setattr(
        DiagnosticOperation, "to_dict", lambda self: (_ for _ in ()).throw(RuntimeError())
    )
    assert not rec.record("t", operation())
    assert rec.snapshot()["record_failures"] == 1
    assert rec.seal_task("t", "failed")


def test_closed_schemas_redaction_and_durable_public_serialization() -> None:
    with pytest.raises(TypeError):
        DiagnosticOperation("agent.execution", 1, 0, **{"attributes": {"secret": "x"}})
    with pytest.raises(ValueError):
        operation("agent.execution", runtime_family="provider-model-id")

    rec, _ = recorder()
    assert rec.admit_task("task-sentinel", "acp")
    assert rec.record("task-sentinel", operation("agent.execution", runtime_family="acp"))
    assert rec.seal_task("task-sentinel", "succeeded")
    snapshot = json.dumps(rec.snapshot(), sort_keys=True)
    task = json.dumps(rec.task_projection("task-sentinel"), sort_keys=True)
    assert "trace_id" not in snapshot and "span_id" not in snapshot
    for forbidden in ("correlation_id", "workdir", "path", "model_id", "session_id", "attributes"):
        assert forbidden not in task
    assert "trace_id" not in task and "root_span_id" not in task

    from serverfs_agent_bridge.models import BridgeEvent, TaskRecord

    assert "trace_id" not in TaskRecord.__dataclass_fields__
    assert "diagnostic" not in TaskRecord.__dataclass_fields__
    assert "trace_id" not in BridgeEvent.__dataclass_fields__
    assert "diagnostic" not in BridgeEvent.__dataclass_fields__
    event_json = json.dumps(
        BridgeEvent(1, "task-sentinel", None, "task.started", {}, "now").to_dict()
    )
    assert "trace_id" not in event_json and "span_id" not in event_json
    diagnostic_json = snapshot + task + event_json
    for forbidden_value in (
        "/private/workdir",
        "prompt-sentinel",
        "provider-output-sentinel",
        "model-sentinel",
        "session-sentinel",
        "correlation-sentinel",
    ):
        assert forbidden_value not in diagnostic_json


def test_diagnostics_module_has_no_persistence_or_telemetry_dependency() -> None:
    bridge_root = Path(__file__).parents[1]
    source = (bridge_root / "src/serverfs_agent_bridge/diagnostics.py").read_text()
    root_manifest = (bridge_root.parent / "pyproject.toml").read_text().lower()
    bridge_manifest = (bridge_root / "pyproject.toml").read_text().lower()
    assert "sqlite" not in source.lower()
    assert "opentelemetry" not in root_manifest
    assert "opentelemetry" not in bridge_manifest


def test_corrupt_internal_state_fails_boundedly_and_snapshot_remains_available() -> None:
    rec, _ = recorder()
    assert rec.admit_task("task", "acp")
    rec._slots["task"].operations = [object()]  # simulate unexpected in-memory corruption
    projection = rec.task_projection("task")
    assert projection == {
        "catalog_version": diagnostics.CATALOG_VERSION,
        "disposition": "not_retained",
        "reason": "recording_unavailable",
    }
    snapshot = rec.snapshot()
    assert snapshot["catalog_version"] == diagnostics.CATALOG_VERSION
    assert snapshot["record_failures"] >= 1

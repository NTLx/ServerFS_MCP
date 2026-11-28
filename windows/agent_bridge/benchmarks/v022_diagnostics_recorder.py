"""Deterministic in-process microbench for the Phase B diagnostics recorder."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from serverfs_agent_bridge.diagnostics import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_RECORDS,
    MAX_TASK_DIAGNOSTIC_RECORDS,
    DiagnosticOperation,
    DiagnosticRecorder,
)


def percentile95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def measure(actions: list[object]) -> float:
    samples = []
    for action in actions:
        started = time.perf_counter_ns()
        action()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return percentile95(samples)


def _terminal_slot(task_id: str, *, full: bool = False) -> tuple[DiagnosticRecorder, str]:
    recorder = DiagnosticRecorder()
    recorder.admit_task(task_id, "acp")
    operation_count = MAX_TASK_DIAGNOSTIC_RECORDS - 1 if full else 1
    for sequence in range(operation_count):
        recorder.record(
            task_id,
            DiagnosticOperation("agent.execution", 0.001, sequence, runtime_family="acp"),
        )
    return recorder, task_id


def run(iterations: int) -> dict[str, float]:
    samples = max(1, iterations)
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "v022_diagnostic_catalog_v1.json").read_text()
    )
    execution_example = next(
        item for item in fixture["example_operations"] if item["kind"] == "agent.execution"
    )

    def make_execution(sequence: int) -> DiagnosticOperation:
        return DiagnosticOperation(
            kind=execution_example["kind"],
            duration_seconds=execution_example["duration_seconds"],
            sequence=sequence,
            runtime_family=execution_example["runtime_family"],
        )

    # Setup is excluded: each action records one operation into a fresh pre-admitted slot.
    ordinary = []
    for index in range(samples):
        rec = DiagnosticRecorder()
        task_id = f"ordinary-{index}"
        rec.admit_task(task_id, "acp")
        ordinary.append(lambda rec=rec, task_id=task_id: rec.record(task_id, make_execution(0)))

    # Setup is excluded: each timed call only seals its own pre-recorded active slot.
    seals = []
    for index in range(samples):
        rec, task_id = _terminal_slot(f"seal-{index}")
        seals.append(lambda rec=rec, task_id=task_id: rec.seal_task(task_id, "succeeded"))

    # Each iteration prepares 2047 terminal seals and one active slot. The timed record()
    # forces exactly one whole-terminal eviction to preserve the active slot's seal reserve.
    evictions = []
    eviction_recorders = []
    for sample in range(samples):
        rec = DiagnosticRecorder()
        for index in range(MAX_DIAGNOSTIC_RECORDS - 1):
            task_id = f"terminal-{sample}-{index}"
            rec.admit_task(task_id, "acp")
            rec.seal_task(task_id, "succeeded", completed_at=float(index))
        task_id = f"eviction-active-{sample}"
        rec.admit_task(task_id, "acp")
        assert rec.retained_record_count + rec._reserved_records == MAX_DIAGNOSTIC_RECORDS

        def evict_and_record(rec=rec, task_id=task_id):
            return rec.record(task_id, make_execution(0))

        evictions.append(evict_and_record)
        eviction_recorders.append(rec)

    # Fixed bounded setup: fill the global record + reservation ceiling, then assert it.
    saturated = DiagnosticRecorder()
    for index in range(MAX_DIAGNOSTIC_RECORDS):
        task_id = f"saturated-{index}"
        assert saturated.admit_task(task_id, "acp")
        assert saturated.record(task_id, DiagnosticOperation("agent.execution", 0.01, 0))
        saturated.seal_task(task_id, "succeeded", completed_at=float(index))
    assert saturated.retained_record_count == MAX_DIAGNOSTIC_RECORDS
    assert saturated.retained_record_count + saturated._reserved_records == MAX_DIAGNOSTIC_RECORDS
    assert saturated.retained_bytes <= MAX_DIAGNOSTIC_BYTES
    snapshot_actions = [saturated.snapshot for _ in range(samples)]

    # Projection is measured at the 64-record task ceiling: 63 ordinary records + seal.
    projection, projection_task = _terminal_slot("maximum-task", full=True)
    projection.seal_task(projection_task, "succeeded")
    assert projection._slots[projection_task].retained_records == MAX_TASK_DIAGNOSTIC_RECORDS
    projection_actions = [
        lambda: projection.task_projection(projection_task) for _ in range(samples)
    ]

    eviction_p95 = measure(evictions)
    assert all(rec.completed_task_evictions == 1 for rec in eviction_recorders)

    return {
        "ordinary_non_evicting_record_p95_ms": measure(ordinary),
        "terminal_seal_write_p95_ms": measure(seals),
        "whole_completed_task_eviction_then_record_p95_ms": eviction_p95,
        "saturated_snapshot_p95_ms": measure(snapshot_actions),
        "maximum_retained_task_projection_p95_ms": measure(projection_actions),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    runs = [run(args.iterations) for _ in range(args.runs)]
    for name in runs[0]:
        values = [entry[name] for entry in runs]
        print(f"{name}: runs={values} median_p95_ms={statistics.median(values):.6f}")


if __name__ == "__main__":
    main()

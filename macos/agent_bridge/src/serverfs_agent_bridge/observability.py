"""Bounded, process-local observability for the Agent Bridge."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import TERMINAL_STATUSES, TaskStatus

RUNTIME_FAMILIES = frozenset({"codex", "claude", "qoder", "acp", "unknown"})
OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
RECOVERY_OUTCOMES = frozenset({"success", "failure", "unknown"})
REASON_CODES = frozenset(
    {
        "ok",
        "safe",
        "absent",
        "disabled",
        "not_applicable",
        "unsupported",
        "configured_not_checked",
        "stale",
        "probe_failed",
        "invalid_configuration",
        "not_ready",
        "unavailable",
        "degraded",
        "unsafe",
        "busy",
        "recovery_guard",
        "stop_unproven",
        "required_component_missing",
        "required_component_unavailable",
        "required_component_degraded",
        "required_component_unknown",
        "optional_component_degraded",
        "optional_component_unknown",
        "all_required_healthy",
        "unknown_value",
    }
)

METRIC_CATALOG: dict[str, dict[str, Any]] = {
    "serverfs_agent_tasks_total": {
        "type": "counter",
        "unit": "total",
        "labels": {
            "outcome": OUTCOMES,
            "runtime_family": RUNTIME_FAMILIES,
            "mode": frozenset({"review", "workspace-write"}),
        },
    },
    "serverfs_agent_task_duration_seconds": {
        "type": "histogram",
        "unit": "seconds",
        "labels": {
            "outcome": OUTCOMES,
            "runtime_family": RUNTIME_FAMILIES,
            "mode": frozenset({"review", "workspace-write"}),
        },
    },
    "serverfs_agent_queue_depth": {
        "type": "gauge",
        "unit": "items",
        "labels": {
            "queue_class": frozenset({"runnable", "waiting_interaction"}),
            "runtime_family": RUNTIME_FAMILIES,
        },
    },
    "serverfs_agent_queue_wait_seconds": {
        "type": "histogram",
        "unit": "seconds",
        "labels": {"runtime_family": RUNTIME_FAMILIES, "outcome": OUTCOMES},
    },
    "serverfs_agent_lease_acquisitions_total": {
        "type": "counter",
        "unit": "total",
        "labels": {
            "outcome": frozenset({"acquired", "busy", "failure"}),
            "lease_class": frozenset({"writer"}),
        },
    },
    "serverfs_agent_lease_wait_seconds": {
        "type": "histogram",
        "unit": "seconds",
        "labels": {
            "outcome": frozenset({"acquired", "busy", "failure"}),
            "lease_class": frozenset({"writer"}),
        },
    },
    "serverfs_agent_recovery_total": {
        "type": "counter",
        "unit": "total",
        "labels": {
            "action": frozenset({"reconcile", "resume", "interrupt", "guard"}),
            "outcome": RECOVERY_OUTCOMES,
            "reason_code": REASON_CODES,
        },
    },
}

MAX_POSSIBLE_SERIES = sum(
    math.prod(len(domain) for domain in definition["labels"].values())
    for definition in METRIC_CATALOG.values()
)
if MAX_POSSIBLE_SERIES > 10_000:
    raise RuntimeError("Agent observability metric catalog exceeds the series ceiling")


def normalize_runtime_family(runtime: str) -> str:
    if runtime in {"codex", "claude", "qoder"}:
        return runtime
    if runtime.startswith("acp-"):
        return "acp"
    return "unknown"


def validate_labels(name: str, labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    definition = METRIC_CATALOG.get(name)
    if definition is None:
        raise ValueError("unknown metric name")
    domains = definition["labels"]
    if set(labels) != set(domains):
        raise ValueError("metric labels do not match catalog")
    for key, value in labels.items():
        if value not in domains[key]:
            raise ValueError("metric label value is outside its domain")
    return tuple((key, labels[key]) for key in domains)


@dataclass
class _Histogram:
    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)


class BridgeObservationMetrics:
    """Metrics with storage bounded by the closed catalog label domains."""

    def __init__(self) -> None:
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self.histograms: dict[tuple[str, tuple[tuple[str, str], ...]], _Histogram] = {}

    def _increment(self, name: str, labels: dict[str, str]) -> None:
        key = (name, validate_labels(name, labels))
        self.counters[key] = self.counters.get(key, 0) + 1

    def _observe(self, name: str, labels: dict[str, str], value: float) -> None:
        if not math.isfinite(value) or value < 0:
            return
        key = (name, validate_labels(name, labels))
        self.histograms.setdefault(key, _Histogram()).add(value)

    def observe_terminal(self, task_record: Any) -> None:
        status = TaskStatus(task_record.status)
        if status not in TERMINAL_STATUSES:
            raise ValueError("task status must be terminal")
        outcome = status.value
        family = normalize_runtime_family(task_record.runtime)
        mode = task_record.profile
        labels = {"outcome": outcome, "runtime_family": family, "mode": mode}
        self._increment("serverfs_agent_tasks_total", labels)
        execution = _duration(task_record.started_at, task_record.completed_at)
        if execution is not None:
            self._observe("serverfs_agent_task_duration_seconds", labels, execution)
        queue_wait = _duration(task_record.created_at, task_record.started_at)
        if queue_wait is not None:
            self._observe(
                "serverfs_agent_queue_wait_seconds",
                {"runtime_family": family, "outcome": outcome},
                queue_wait,
            )

    def observe_lease(self, outcome: str, duration_seconds: float) -> None:
        labels = {"outcome": outcome, "lease_class": "writer"}
        self._increment("serverfs_agent_lease_acquisitions_total", labels)
        self._observe("serverfs_agent_lease_wait_seconds", labels, duration_seconds)

    def observe_recovery(self, action: str, outcome: str, reason_code: str) -> None:
        self._increment(
            "serverfs_agent_recovery_total",
            {"action": action, "outcome": outcome, "reason_code": reason_code},
        )

    def snapshot(self, store: Any, runtime_names: Any) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for (name, label_tuple), value in self.counters.items():
            rows.append(
                {
                    "name": name,
                    "type": "counter",
                    "unit": "total",
                    "labels": dict(label_tuple),
                    "value": value,
                }
            )
        for (name, label_tuple), value in self.histograms.items():
            rows.append(
                {
                    "name": name,
                    "type": "histogram",
                    "unit": "seconds",
                    "labels": dict(label_tuple),
                    "count": value.count,
                    "sum": value.total,
                    "min": value.minimum,
                    "max": value.maximum,
                }
            )
        depths: dict[tuple[str, str], int] = {}
        for runtime in runtime_names:
            family = normalize_runtime_family(runtime)
            for queue_class in ("runnable", "waiting_interaction"):
                depths.setdefault((queue_class, family), 0)
        for task in store.list_nonterminal_tasks():
            status = TaskStatus(task.status)
            queue_class = (
                "runnable"
                if status is TaskStatus.QUEUED
                else "waiting_interaction"
                if status in {TaskStatus.WAITING_FOR_APPROVAL, TaskStatus.WAITING_FOR_QUESTION}
                else None
            )
            if queue_class is not None:
                key = (queue_class, normalize_runtime_family(task.runtime))
                depths[key] = depths.get(key, 0) + 1
        for (queue_class, family), value in depths.items():
            rows.append(
                {
                    "name": "serverfs_agent_queue_depth",
                    "type": "gauge",
                    "unit": "items",
                    "labels": {"queue_class": queue_class, "runtime_family": family},
                    "value": value,
                }
            )
        rows.sort(key=lambda row: (row["name"], tuple(row["labels"].items())))
        return {"schema_version": 1, "metrics": rows}


def _duration(start: str | None, end: str | None) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(end.replace("Z", "+00:00"))
        seconds = (end_time - start_time).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None

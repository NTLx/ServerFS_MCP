"""Typed, bounded, process-local Agent diagnostics (catalog v1)."""

from __future__ import annotations

import heapq
import json
import math
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .observability import OUTCOMES, REASON_CODES, RECOVERY_OUTCOMES, RUNTIME_FAMILIES

CATALOG_VERSION = 1
MAX_DIAGNOSTIC_RECORDS = 2_048
MAX_DIAGNOSTIC_BYTES = 1_048_576
MAX_TASK_DIAGNOSTIC_RECORDS = 64
MAX_TASK_DIAGNOSTIC_BYTES = 32_768
MAX_DIAGNOSTIC_OPERATION_BYTES = 1_024
TERMINAL_SEAL_RESERVED_BYTES = 512

OPERATION_KINDS = frozenset(
    {
        "agent.queue_wait",
        "agent.lease_acquire",
        "agent.runtime_start",
        "agent.execution",
        "agent.waiting_external",
        "agent.finalization",
        "agent.recovery",
    }
)
COMPLETENESS = frozenset(
    {"complete", "truncated_prefix", "restarted", "restarted_truncated_prefix"}
)
NOT_RETAINED_REASONS = frozenset(
    {"evicted", "bridge_restarted", "capacity_exhausted", "recording_unavailable"}
)
LEASE_OUTCOMES = frozenset({"acquired", "busy", "failure"})
INTERACTION_CLASSES = frozenset({"approval", "question"})
_SEAL_REASON = {
    "succeeded": "ok",
    "failed": "degraded",
    "cancelled": "safe",
    "interrupted": "unavailable",
}
_MAX_COUNTER = (1 << 63) - 1


class QueryDisposition(StrEnum):
    AVAILABLE = "available"
    UNKNOWN_TASK = "unknown_task"
    NOT_RETAINED = "not_retained"
    BRIDGE_UNAVAILABLE = "bridge_unavailable"


@dataclass(frozen=True)
class DiagnosticTraceContext:
    """Opaque context minted only by the Bridge; IDs never leave this module's recorder."""

    trace_id: str
    root_span_id: str

    @classmethod
    def generate(cls) -> DiagnosticTraceContext:
        return cls(secrets.token_hex(16), secrets.token_hex(8))


@dataclass(frozen=True)
class DiagnosticStaging:
    """Bridge-local pre-creation state, attachable only after durable task creation."""

    context: DiagnosticTraceContext
    started_at: float


@dataclass(frozen=True)
class DiagnosticOperation:
    kind: str
    duration_seconds: float
    sequence: int
    outcome: str | None = None
    reason_code: str | None = None
    runtime_family: str | None = None
    interaction_class: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in OPERATION_KINDS:
            raise ValueError("operation kind is outside catalog")
        if (
            isinstance(self.duration_seconds, bool)
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds < 0
        ):
            raise ValueError("duration must be nonnegative")
        if self.sequence < 0:
            raise ValueError("sequence must be nonnegative")
        if self.outcome is not None:
            domain = (
                LEASE_OUTCOMES
                if self.kind == "agent.lease_acquire"
                else RECOVERY_OUTCOMES
                if self.kind == "agent.recovery"
                else OUTCOMES | {"started"}
            )
            if self.outcome not in domain:
                raise ValueError("outcome is outside catalog")
        if self.reason_code is not None and self.reason_code not in REASON_CODES:
            raise ValueError("reason code is outside catalog")
        if self.runtime_family is not None and self.runtime_family not in RUNTIME_FAMILIES:
            raise ValueError("runtime family is outside catalog")
        if self.interaction_class is not None and self.interaction_class not in INTERACTION_CLASSES:
            raise ValueError("interaction class is outside catalog")
        if (self.kind == "agent.waiting_external") != (self.interaction_class is not None):
            raise ValueError("waiting_external requires only interaction_class")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "duration_seconds": self.duration_seconds,
            "sequence": self.sequence,
        }
        for name in ("outcome", "reason_code", "runtime_family", "interaction_class"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


@dataclass(frozen=True)
class DiagnosticSeal:
    outcome: str
    reason_code: str
    wall_clock_seconds: float
    terminal_completed_at: float
    completeness: str

    def __post_init__(self) -> None:
        if self.outcome not in _SEAL_REASON or self.reason_code != _SEAL_REASON[self.outcome]:
            raise ValueError("seal outcome/reason does not match catalog")
        if (
            not math.isfinite(self.wall_clock_seconds)
            or not math.isfinite(self.terminal_completed_at)
            or self.wall_clock_seconds < 0
            or self.completeness not in COMPLETENESS
        ):
            raise ValueError("invalid terminal seal")

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "wall_clock_seconds": self.wall_clock_seconds,
            "terminal_completed_at": self.terminal_completed_at,
            "completeness": self.completeness,
        }


@dataclass
class _TaskSlot:
    task_id: str
    context: DiagnosticTraceContext
    runtime_family: str
    started_at: float
    restarted: bool
    operations: list[DiagnosticOperation] = field(default_factory=list)
    operation_bytes: int = 0
    sealed: DiagnosticSeal | None = None
    seal_bytes: int = 0
    truncated: bool = False

    @property
    def retained_records(self) -> int:
        return len(self.operations) + (self.sealed is not None)

    @property
    def terminal(self) -> bool:
        return self.sealed is not None

    @property
    def completeness(self) -> str:
        prefix = self.restarted
        if self.truncated:
            return "restarted_truncated_prefix" if prefix else "truncated_prefix"
        return "restarted" if prefix else "complete"


def _json_bytes(value: dict[str, Any]) -> int:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return len(encoded.encode())


class DiagnosticRecorder:
    """Synchronous recorder. All retained payload and indexes have hard bounds."""

    def __init__(self, *, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self.started_at = datetime.now(UTC).isoformat()
        self._slots: OrderedDict[str, _TaskSlot] = OrderedDict()
        self._not_retained: OrderedDict[str, str] = OrderedDict()
        self._retained_records = 0
        self._retained_bytes = 0
        self._reserved_records = 0
        self._terminal_heap: list[tuple[float, int, str]] = []
        self._seal_order = 0
        self.record_failures = 0
        self.admission_failures = 0
        self.ordinary_records_dropped = 0
        self.oversized_operation_rejections = 0
        self.completed_task_evictions = 0

    @property
    def retained_record_count(self) -> int:
        return self._retained_records

    @property
    def retained_bytes(self) -> int:
        return self._retained_bytes

    @property
    def _reserved_bytes(self) -> int:
        return self._reserved_records * TERMINAL_SEAL_RESERVED_BYTES

    def _remember_not_retained(self, task_id: str, reason: str) -> None:
        self._not_retained[task_id] = reason
        self._not_retained.move_to_end(task_id)
        while len(self._not_retained) > MAX_DIAGNOSTIC_RECORDS:
            self._not_retained.popitem(last=False)

    def _bump(self, name: str) -> None:
        setattr(self, name, min(_MAX_COUNTER, getattr(self, name) + 1))

    def _evict_oldest_terminal(self, except_task: str | None = None) -> bool:
        deferred: list[tuple[float, int, str]] = []
        oldest: tuple[float, int, str] | None = None
        while self._terminal_heap:
            candidate = heapq.heappop(self._terminal_heap)
            _, _, task_id = candidate
            slot = self._slots.get(task_id)
            if slot is None or not slot.terminal or slot.sealed is None:
                continue
            if task_id == except_task:
                deferred.append(candidate)
                continue
            oldest = candidate
            break
        for candidate in deferred:
            heapq.heappush(self._terminal_heap, candidate)
        if oldest is None:
            return False
        task_id = oldest[2]
        slot = self._slots.pop(task_id)
        self._retained_records -= slot.retained_records
        self._retained_bytes -= slot.operation_bytes + slot.seal_bytes
        if not slot.terminal:
            self._reserved_records -= 1
        self._remember_not_retained(task_id, "evicted")
        self._bump("completed_task_evictions")
        return True

    def stage_submission(self) -> DiagnosticStaging:
        return DiagnosticStaging(DiagnosticTraceContext.generate(), self._clock())

    def admit_task(
        self,
        task_id: str,
        runtime_family: str,
        *,
        restarted: bool = False,
        staging: DiagnosticStaging | None = None,
    ) -> bool:
        try:
            if task_id in self._slots or task_id in self._not_retained:
                return False
            if runtime_family not in RUNTIME_FAMILIES:
                runtime_family = "unknown"
            while (
                self._retained_records + self._reserved_records + 1 > MAX_DIAGNOSTIC_RECORDS
                or self.retained_bytes + self._reserved_bytes + TERMINAL_SEAL_RESERVED_BYTES
                > MAX_DIAGNOSTIC_BYTES
            ):
                if not self._evict_oldest_terminal():
                    self._bump("admission_failures")
                    self._remember_not_retained(task_id, "capacity_exhausted")
                    return False
            self._not_retained.pop(task_id, None)
            self._slots[task_id] = _TaskSlot(
                task_id,
                staging.context if staging is not None else DiagnosticTraceContext.generate(),
                runtime_family,
                staging.started_at if staging is not None else self._clock(),
                restarted,
            )
            self._reserved_records += 1
            return True
        except Exception:
            self._bump("record_failures")
            self._bump("admission_failures")
            return False

    def record(self, task_id: str, operation: DiagnosticOperation) -> bool:
        slot = self._slots.get(task_id)
        if slot is None or slot.terminal or slot.truncated:
            return False
        try:
            if operation.sequence != len(slot.operations):
                self._bump("record_failures")
                self._truncate(slot)
                return False
            payload_size = _json_bytes(operation.to_dict())
            if payload_size > MAX_DIAGNOSTIC_OPERATION_BYTES:
                self._bump("oversized_operation_rejections")
                self._truncate(slot)
                return False
            while (
                slot.retained_records + 2 > MAX_TASK_DIAGNOSTIC_RECORDS
                or slot.operation_bytes + payload_size + TERMINAL_SEAL_RESERVED_BYTES
                > MAX_TASK_DIAGNOSTIC_BYTES
                or self.retained_record_count + self._reserved_records + 1 > MAX_DIAGNOSTIC_RECORDS
                or self.retained_bytes + self._reserved_bytes + payload_size > MAX_DIAGNOSTIC_BYTES
            ):
                if self._evict_oldest_terminal(except_task=task_id):
                    continue
                self._truncate(slot)
                return False
            slot.operations.append(operation)
            slot.operation_bytes += payload_size
            self._retained_records += 1
            self._retained_bytes += payload_size
            return True
        except Exception:
            self._bump("record_failures")
            self._truncate(slot)
            return False

    def _truncate(self, slot: _TaskSlot) -> None:
        if not slot.truncated:
            slot.truncated = True
            self._bump("ordinary_records_dropped")

    def seal_task(self, task_id: str, outcome: str, *, completed_at: float | None = None) -> bool:
        slot = self._slots.get(task_id)
        if slot is None or slot.sealed is not None:
            return False
        try:
            ended = self._clock() if completed_at is None else completed_at
            seal = DiagnosticSeal(
                outcome=outcome,
                reason_code=_SEAL_REASON[outcome],
                wall_clock_seconds=max(0.0, ended - slot.started_at),
                terminal_completed_at=ended,
                completeness=slot.completeness,
            )
            size = _json_bytes(seal.to_dict())
            if size > TERMINAL_SEAL_RESERVED_BYTES:
                raise ValueError("terminal seal exceeds reserved capacity")
            slot.sealed = seal
            slot.seal_bytes = size
            self._reserved_records -= 1
            self._retained_records += 1
            self._retained_bytes += size
            self._seal_order += 1
            heapq.heappush(
                self._terminal_heap,
                (seal.terminal_completed_at, self._seal_order, task_id),
            )
            return True
        except Exception:
            self._bump("record_failures")
            return False

    def task_projection(self, task_id: str, *, durable_exists: bool = True) -> dict[str, Any]:
        slot = self._slots.get(task_id)
        if slot is None:
            if not durable_exists:
                disposition = QueryDisposition.UNKNOWN_TASK.value
                reason = None
            else:
                disposition = QueryDisposition.NOT_RETAINED.value
                reason = self._not_retained.get(task_id)
            result: dict[str, Any] = {
                "catalog_version": CATALOG_VERSION,
                "disposition": disposition,
            }
            if reason is not None and disposition == QueryDisposition.NOT_RETAINED.value:
                result["reason"] = reason
            return result

        try:
            durations = [op.duration_seconds for op in slot.operations]
            accounted = sum(durations)
            wall = (
                slot.sealed.wall_clock_seconds
                if slot.sealed
                else max(0.0, self._clock() - slot.started_at)
            )
            lease_wait = sum(
                op.duration_seconds
                for op in slot.operations
                if op.kind == "agent.lease_acquire" and op.outcome == "acquired"
            )
            return {
                "catalog_version": CATALOG_VERSION,
                "disposition": QueryDisposition.AVAILABLE.value,
                "task_id": task_id,
                "runtime_family": slot.runtime_family,
                "status": slot.sealed.outcome if slot.sealed else "active",
                "completeness": slot.completeness,
                "operations": [op.to_dict() for op in slot.operations],
                "terminal_seal": slot.sealed.to_dict() if slot.sealed else None,
                "rollups": {
                    "wall_clock_seconds": wall,
                    "accounted_seconds": accounted,
                    "unaccounted_seconds": wall - accounted,
                    "active_execution_seconds": sum(
                        op.duration_seconds
                        for op in slot.operations
                        if op.kind == "agent.execution"
                    ),
                    "external_wait_seconds": sum(
                        op.duration_seconds
                        for op in slot.operations
                        if op.kind == "agent.waiting_external"
                    ),
                    "acquired_lease_wait_seconds": lease_wait,
                },
            }
        except Exception:
            self._bump("record_failures")
            return {
                "catalog_version": CATALOG_VERSION,
                "disposition": QueryDisposition.NOT_RETAINED.value,
                "reason": "recording_unavailable",
            }

    def snapshot(self) -> dict[str, Any]:
        slots = list(self._slots.values())
        return {
            "catalog_version": CATALOG_VERSION,
            "buffer_started_at": self.started_at,
            "retained_record_count": self.retained_record_count,
            "retained_bytes": self.retained_bytes,
            "retained_task_slot_count": len(slots),
            "active_task_count": sum(not slot.terminal for slot in slots),
            "terminal_task_count": sum(slot.terminal for slot in slots),
            "truncated_task_count": sum(slot.truncated for slot in slots),
            "completed_task_evictions": self.completed_task_evictions,
            "admission_failures": self.admission_failures,
            "ordinary_records_dropped": self.ordinary_records_dropped,
            "oversized_operation_rejections": self.oversized_operation_rejections,
            "record_failures": self.record_failures,
        }

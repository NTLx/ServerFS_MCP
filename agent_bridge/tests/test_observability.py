from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.lease_identity import slot_lease_id
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import TERMINAL_STATUSES, AgentMode, TaskStatus
from serverfs_agent_bridge.observability import (
    MAX_POSSIBLE_SERIES,
    METRIC_CATALOG,
    BridgeObservationMetrics,
    normalize_runtime_family,
    validate_labels,
)
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore


def _record(status: str, **overrides: object) -> SimpleNamespace:
    fields = {
        "task_id": "sentinel-task-id",
        "runtime": "fake",
        "workdir_alias": "sentinel-workdir",
        "profile": "workspace-write",
        "status": status,
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:00:02Z",
        "completed_at": "2026-01-01T00:00:05Z",
        "requested_model": "sentinel-model",
        "correlation_id": "sentinel-correlation",
        "error_message": "sentinel-error",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _row(snapshot: dict, name: str, labels: dict[str, str]) -> dict:
    return next(
        row for row in snapshot["metrics"] if row["name"] == name and row["labels"] == labels
    )


def test_phase_b_catalog_domains_and_series_ceiling() -> None:
    assert set(METRIC_CATALOG) == {
        "serverfs_agent_tasks_total",
        "serverfs_agent_task_duration_seconds",
        "serverfs_agent_queue_depth",
        "serverfs_agent_queue_wait_seconds",
        "serverfs_agent_lease_acquisitions_total",
        "serverfs_agent_lease_wait_seconds",
        "serverfs_agent_recovery_total",
    }
    assert MAX_POSSIBLE_SERIES == 416
    assert MAX_POSSIBLE_SERIES <= 10_000
    expected_labels = {
        "serverfs_agent_tasks_total": {"outcome", "runtime_family", "mode"},
        "serverfs_agent_task_duration_seconds": {"outcome", "runtime_family", "mode"},
        "serverfs_agent_queue_depth": {"queue_class", "runtime_family"},
        "serverfs_agent_queue_wait_seconds": {"runtime_family", "outcome"},
        "serverfs_agent_lease_acquisitions_total": {"outcome", "lease_class"},
        "serverfs_agent_lease_wait_seconds": {"outcome", "lease_class"},
        "serverfs_agent_recovery_total": {"action", "outcome", "reason_code"},
    }
    assert {
        name: set(definition["labels"]) for name, definition in METRIC_CATALOG.items()
    } == expected_labels
    outcomes = {"succeeded", "failed", "cancelled", "interrupted"}
    families = {"codex", "claude", "qoder", "acp", "unknown"}
    reasons = {
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
    for name in ("serverfs_agent_tasks_total", "serverfs_agent_task_duration_seconds"):
        assert METRIC_CATALOG[name]["labels"] == {
            "outcome": outcomes,
            "runtime_family": families,
            "mode": {"review", "workspace-write"},
        }
    assert METRIC_CATALOG["serverfs_agent_queue_depth"]["labels"] == {
        "queue_class": {"runnable", "waiting_interaction"},
        "runtime_family": families,
    }
    assert METRIC_CATALOG["serverfs_agent_queue_wait_seconds"]["labels"] == {
        "runtime_family": families,
        "outcome": outcomes,
    }
    for name in ("serverfs_agent_lease_acquisitions_total", "serverfs_agent_lease_wait_seconds"):
        assert METRIC_CATALOG[name]["labels"] == {
            "outcome": {"acquired", "busy", "failure"},
            "lease_class": {"writer"},
        }
    assert METRIC_CATALOG["serverfs_agent_recovery_total"]["labels"] == {
        "action": {"reconcile", "resume", "interrupt", "guard"},
        "outcome": {"success", "failure", "unknown"},
        "reason_code": reasons,
    }
    name = "serverfs_agent_tasks_total"
    labels = {"outcome": "succeeded", "runtime_family": "codex", "mode": "review"}
    assert len(validate_labels(name, labels)) == 3
    for invalid_name, invalid_labels in (
        ("unknown", labels),
        (name, {"outcome": "succeeded", "runtime_family": "codex"}),
        (name, labels | {"extra": "x"}),
        (name, labels | {"runtime_family": "fake"}),
        (name, labels | {"mode": "unexpected"}),
    ):
        with pytest.raises(ValueError):
            validate_labels(invalid_name, invalid_labels)


def test_runtime_family_normalization_and_acp_collapse() -> None:
    assert normalize_runtime_family("codex") == "codex"
    assert normalize_runtime_family("claude") == "claude"
    assert normalize_runtime_family("qoder") == "qoder"
    assert normalize_runtime_family("acp-opencode") == "acp"
    assert normalize_runtime_family("acp-other-profile") == "acp"
    assert normalize_runtime_family("fake") == "unknown"


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled", "interrupted"])
def test_terminal_metrics_are_bounded_and_record_durations(outcome: str) -> None:
    metrics = BridgeObservationMetrics()
    metrics.observe_terminal(_record(outcome, runtime="acp-profile-one", profile="review"))
    snapshot = metrics.snapshot(
        SimpleNamespace(list_nonterminal_tasks=lambda: []), ["acp-profile-one", "acp-profile-two"]
    )
    labels = {"outcome": outcome, "runtime_family": "acp", "mode": "review"}
    assert _row(snapshot, "serverfs_agent_tasks_total", labels)["value"] == 1
    duration = _row(snapshot, "serverfs_agent_task_duration_seconds", labels)
    assert (duration["count"], duration["sum"], duration["min"], duration["max"]) == (1, 3, 3, 3)
    queue = _row(
        snapshot, "serverfs_agent_queue_wait_seconds", {"runtime_family": "acp", "outcome": outcome}
    )
    assert (queue["count"], queue["sum"]) == (1, 2)
    encoded = json.dumps(snapshot)
    for secret in (
        "sentinel-task-id",
        "sentinel-workdir",
        "sentinel-model",
        "sentinel-correlation",
        "sentinel-error",
    ):
        assert secret not in encoded


@pytest.mark.parametrize(
    "case",
    [
        ({"created_at": "bad"}, {"serverfs_agent_task_duration_seconds"}),
        ({"started_at": None}, set()),
        ({"completed_at": "bad"}, {"serverfs_agent_queue_wait_seconds"}),
        ({"started_at": "2026-01-01T00:00:06Z"}, {"serverfs_agent_queue_wait_seconds"}),
    ],
)
def test_invalid_timestamp_pairs_do_not_break_terminal_count(case: tuple[dict, set[str]]) -> None:
    overrides, expected_histograms = case
    metrics = BridgeObservationMetrics()
    metrics.observe_terminal(_record("succeeded", **overrides))
    snapshot = metrics.snapshot(SimpleNamespace(list_nonterminal_tasks=lambda: []), [])
    labels = {"outcome": "succeeded", "runtime_family": "unknown", "mode": "workspace-write"}
    assert _row(snapshot, "serverfs_agent_tasks_total", labels)["value"] == 1
    assert {
        row["name"] for row in snapshot["metrics"] if row["type"] == "histogram"
    } == expected_histograms


def test_snapshot_queue_depth_counts_only_queued_and_waiting() -> None:
    tasks = [
        _record("queued", runtime="codex", profile="review"),
        _record("waiting_for_approval", runtime="acp-a", profile="workspace-write"),
        _record("waiting_for_question", runtime="acp-b", profile="review"),
        _record("running", runtime="codex", profile="review"),
    ]
    store = SimpleNamespace(list_nonterminal_tasks=lambda: tasks)
    snapshot = BridgeObservationMetrics().snapshot(store, ["codex", "acp-a", "acp-b", "claude"])
    assert (
        _row(
            snapshot,
            "serverfs_agent_queue_depth",
            {"queue_class": "runnable", "runtime_family": "codex"},
        )["value"]
        == 1
    )
    assert (
        _row(
            snapshot,
            "serverfs_agent_queue_depth",
            {"queue_class": "waiting_interaction", "runtime_family": "acp"},
        )["value"]
        == 2
    )
    assert (
        _row(
            snapshot,
            "serverfs_agent_queue_depth",
            {"queue_class": "runnable", "runtime_family": "claude"},
        )["value"]
        == 0
    )
    assert not any(row["labels"]["runtime_family"] == "unknown" for row in snapshot["metrics"])


def _service(tmp_path: Path) -> BridgeService:
    root = tmp_path / "repo"
    root.mkdir()
    policy = WorkdirAgentPolicy(
        slot=1,
        alias="repo",
        host_path=root,
        mode=AgentMode.WORKSPACE_WRITE,
        runtimes=frozenset({"fake"}),
        read_only=False,
    )
    return BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=PolicyRegistry([policy]),
        adapters={"fake": FakeAdapter()},
        lease_manager=LeaseManager(tmp_path / "locks", lease_ids=[policy.lease_id]),
    )


async def _wait_terminal(service: BridgeService, task_id: str) -> str:
    import asyncio

    for _ in range(200):
        status = service.store.get_task(task_id).status
        if TaskStatus(status) in TERMINAL_STATUSES:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("task did not become terminal")


@pytest.mark.asyncio
async def test_service_success_counts_once_and_close_does_not_duplicate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="fake", workdir="repo", path="", profile="workspace-write", prompt="complete:ok"
        )
        assert await _wait_terminal(service, submitted["task_id"]) == "succeeded"
        await service.close()
        labels = {"outcome": "succeeded", "runtime_family": "unknown", "mode": "workspace-write"}
        snapshot = service.observability_snapshot()
        assert _row(snapshot, "serverfs_agent_tasks_total", labels)["value"] == 1
        assert _row(snapshot, "serverfs_agent_task_duration_seconds", labels)["count"] == 1
        assert (
            _row(
                snapshot,
                "serverfs_agent_queue_wait_seconds",
                {"runtime_family": "unknown", "outcome": "succeeded"},
            )["count"]
            == 1
        )
        assert (
            _row(
                snapshot,
                "serverfs_agent_lease_acquisitions_total",
                {"outcome": "acquired", "lease_class": "writer"},
            )["value"]
            == 1
        )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_observation_failure_does_not_change_task_state(tmp_path: Path) -> None:
    class FailingMetrics(BridgeObservationMetrics):
        def observe_terminal(self, task_record) -> None:
            raise ValueError("synthetic observability failure")

    service = _service(tmp_path)
    service.observation_metrics = FailingMetrics()
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="fake",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="complete:still-succeeds",
        )
        assert await _wait_terminal(service, submitted["task_id"]) == "succeeded"
        task = service.get_task(submitted["task_id"])
        assert task["status"] == "succeeded"
        assert task["final_response"] == "still-succeeds"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_queued_cancel_and_writer_contention_are_counted_once(tmp_path: Path) -> None:
    import asyncio

    service = _service(tmp_path)
    await service.start()
    try:
        first = await service.submit_task(
            runtime="fake", workdir="repo", path="", profile="workspace-write", prompt="wait:"
        )
        # The active writer makes the next normal submission fail at the existing lease boundary.
        for _ in range(200):
            if service.store.get_task(first["task_id"]).status == "running":
                break
            await asyncio.sleep(0.01)
        with pytest.raises(BridgeError) as busy:
            await service.submit_task(
                runtime="fake",
                workdir="repo",
                path="",
                profile="workspace-write",
                prompt="complete:blocked",
            )
        assert busy.value.code == "WORKDIR_BUSY"
        await service.cancel_task(first["task_id"])
        assert await _wait_terminal(service, first["task_id"]) == "cancelled"
        snapshot = service.observability_snapshot()
        assert (
            _row(
                snapshot,
                "serverfs_agent_tasks_total",
                {"outcome": "cancelled", "runtime_family": "unknown", "mode": "workspace-write"},
            )["value"]
            == 1
        )
        assert (
            _row(
                snapshot,
                "serverfs_agent_lease_acquisitions_total",
                {"outcome": "busy", "lease_class": "writer"},
            )["value"]
            == 1
        )
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_active", "expected_outcome", "expected_reason"),
    [
        (False, "success", "ok"),
        (True, "unknown", "recovery_guard"),
        (None, "unknown", "stop_unproven"),
    ],
)
async def test_guard_reconciliation_metrics_preserve_existing_decision(
    tmp_path: Path, provider_active: bool | None, expected_outcome: str, expected_reason: str
) -> None:
    from serverfs_agent_bridge.adapters.base import ReconcileResult
    from serverfs_agent_bridge.models import ReconciliationStatus

    class ReconcileFake(FakeAdapter):
        async def reconcile_task(self, task):
            return ReconcileResult(
                status=ReconciliationStatus.SESSION_RESUMABLE,
                provider_active=provider_active,
                detail="deterministic test result",
            )

    service = _service(tmp_path)
    service.adapters["fake"] = ReconcileFake()
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="fake",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="complete:done",
        )
        assert await _wait_terminal(service, submitted["task_id"]) == "succeeded"
        service.guard_manager.create(
            lease_id=slot_lease_id(1),
            task_id=submitted["task_id"],
            runtime="fake",
            workdir_alias="repo",
            correlation_id=None,
        )
        if provider_active is False:
            await service._reconcile_guard(slot_lease_id(1))
            assert service.guard_manager.read(slot_lease_id(1)) is None
        else:
            with pytest.raises(BridgeError) as blocked:
                await service._reconcile_guard(slot_lease_id(1))
            assert blocked.value.code == "WORKDIR_RECOVERY_REQUIRED"
            assert service.guard_manager.read(slot_lease_id(1)) is not None
        assert (
            _row(
                service.observability_snapshot(),
                "serverfs_agent_recovery_total",
                {"action": "guard", "outcome": expected_outcome, "reason_code": expected_reason},
            )["value"]
            == 1
        )
    finally:
        await service.close()

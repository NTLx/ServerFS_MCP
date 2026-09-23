from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from serverfs_agent_bridge.adapters.base import (
    AdapterResult,
    ReconcileResult,
    TaskContext,
)
from serverfs_agent_bridge.adapters.fake import FakeAdapter
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode, ReconciliationStatus, TaskStatus
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.protocol import BridgeProtocolServer
from serverfs_agent_bridge.service import BridgeLimits, BridgeService
from serverfs_agent_bridge.store import TaskStore
from serverfs_agent_bridge.util import utc_after, utc_before


class LargeResultAdapter(FakeAdapter):
    async def run_task(self, context: TaskContext) -> AdapterResult:
        if context.prompt.startswith("large:"):
            repeats = int(context.prompt.removeprefix("large:"))
            text = "界abc" * repeats
            return AdapterResult(
                final_response=text,
                native_session_id=f"large-session-{context.task_id}",
                native_turn_id=f"large-turn-{context.task_id}",
            )
        if context.prompt == "oversize":
            text = "x" * (8 * 1024 * 1024 + 1)
            return AdapterResult(
                final_response=text,
                native_session_id=f"large-session-{context.task_id}",
                native_turn_id=f"large-turn-{context.task_id}",
            )
        return await super().run_task(context)


class UnknownRecoveryAdapter(FakeAdapter):
    async def run_task(self, context: TaskContext) -> AdapterResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def cancel(self, task_id: str) -> None:
        return None

    async def reconcile_task(self, task) -> ReconcileResult:
        return ReconcileResult(
            status=ReconciliationStatus.UNKNOWN,
            provider_active=None,
            detail="provider stop cannot be proven",
        )


class CountingFakeAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.runs = 0

    async def run_task(self, context: TaskContext) -> AdapterResult:
        self.runs += 1
        return await super().run_task(context)

    async def reconcile_task(self, task) -> ReconcileResult:
        return ReconcileResult(
            status=ReconciliationStatus.UNKNOWN,
            provider_active=None,
            detail="provider stop cannot be proven after restart",
        )


def make_service(
    tmp_path: Path,
    *,
    adapter: FakeAdapter | None = None,
    limits: BridgeLimits | None = None,
) -> BridgeService:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    policy = WorkdirAgentPolicy(
        slot=1,
        alias="repo",
        host_path=repo,
        mode=AgentMode.WORKSPACE_WRITE,
        runtimes=frozenset({"fake"}),
        read_only=False,
    )
    return BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=PolicyRegistry([policy]),
        adapters={"fake": adapter or FakeAdapter()},
        lease_manager=LeaseManager(tmp_path / "locks"),
        limits=limits,
    )


async def wait_for_status(service: BridgeService, task_id: str, *statuses: str) -> dict:
    for _ in range(700):
        task = service.get_task(task_id)
        if task["status"] in statuses:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError(f"task did not reach {statuses}: {service.get_task(task_id)}")


@pytest.mark.asyncio
async def test_correlation_event_envelope_and_manifest_hash(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start()
    correlation_id = "batch-α-42"
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="complete:hello",
        correlation_id=correlation_id,
    )
    assert submitted["correlation_id"] == correlation_id

    task = await wait_for_status(service, submitted["task_id"], "succeeded")
    assert task["correlation_id"] == correlation_id
    assert task["manifest"]["schema_version"] == 1
    assert task["manifest"]["bridge_version"] == "0.7.1"
    assert task["manifest"]["protocol_version"] == 1
    assert task["manifest"]["correlation_id"] == correlation_id
    assert task["manifest"]["runtime"]["name"] == "fake"
    assert task["manifest"]["workspace"]["alias"] == "repo"
    assert "host_path" not in json.dumps(task["manifest"])

    canonical = json.dumps(
        task["manifest"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert task["manifest_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()

    event_page = service.read_events(submitted["task_id"])
    assert event_page["correlation_id"] == correlation_id
    events = event_page["events"]
    assert events
    assert all(event["schema_version"] == 1 for event in events)
    assert all(event["correlation_id"] == correlation_id for event in events)

    with pytest.raises(BridgeError) as too_large:
        await service.submit_task(
            runtime="fake",
            workdir="repo",
            path="",
            profile="review",
            prompt="complete:no",
            correlation_id="界" * 86,
        )
    assert too_large.value.code == "INVALID_REQUEST"

    with pytest.raises(BridgeError) as control:
        await service.submit_task(
            runtime="fake",
            workdir="repo",
            path="",
            profile="review",
            prompt="complete:no",
            correlation_id="bad\nvalue",
        )
    assert control.value.code == "INVALID_REQUEST"
    await service.close()


@pytest.mark.asyncio
async def test_large_result_spool_exact_utf8_retrieval_and_protocol_dispatch(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path, adapter=LargeResultAdapter())
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="large:50000",
        correlation_id="result-run-1",
    )
    task = await wait_for_status(service, submitted["task_id"], "succeeded")
    expected = ("界abc" * 50000).encode("utf-8")
    assert len(expected) > service.limits.max_final_response_bytes
    assert task["final_response_truncated"] is True
    assert task["result"] == {
        "storage": "spool",
        "size_bytes": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
        "retrievable": True,
    }
    assert len(task["final_response"].encode("utf-8")) <= service.limits.result_preview_bytes

    reconstructed = bytearray()
    offset = 0
    while True:
        chunk = service.read_result(
            submitted["task_id"],
            offset_bytes=offset,
            max_bytes=65_535,
        )
        reconstructed.extend(str(chunk["text"]).encode("utf-8"))
        assert chunk["correlation_id"] == "result-run-1"
        assert chunk["offset_bytes"] == offset
        offset = int(chunk["next_offset_bytes"])
        if chunk["eof"]:
            break
    assert bytes(reconstructed) == expected

    protocol = BridgeProtocolServer(
        service=service,
        socket_path=tmp_path / "run" / "unused.sock",
    )
    final_chunk = await protocol._dispatch(
        "task.result.read",
        {
            "task_id": submitted["task_id"],
            "offset_bytes": 0,
            "max_bytes": 7,
        },
    )
    assert final_chunk["correlation_id"] == "result-run-1"
    assert final_chunk["next_offset_bytes"] <= 7
    assert final_chunk["text"].encode("utf-8") == expected[: final_chunk["next_offset_bytes"]]
    await service.close()


@pytest.mark.asyncio
async def test_result_above_spool_limit_fails(tmp_path: Path) -> None:
    service = make_service(tmp_path, adapter=LargeResultAdapter())
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="oversize",
    )
    task = await wait_for_status(service, submitted["task_id"], "failed")
    assert task["error_code"] == "AGENT_RESULT_TOO_LARGE"
    await service.close()


@pytest.mark.asyncio
async def test_task_timeout_stales_interaction_and_rejects_late_response(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        limits=BridgeLimits(task_timeout_seconds=1),
    )
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="approval:echo timeout",
    )
    waiting = await wait_for_status(
        service,
        submitted["task_id"],
        "waiting_for_approval",
    )
    request = waiting["pending_request"]
    assert request["expires_at"] == waiting["deadline_at"]

    timed_out = await wait_for_status(service, submitted["task_id"], "interrupted")
    assert timed_out["error_code"] == "AGENT_TASK_TIMED_OUT"
    assert service.store.get_request(request["request_id"]).status == "stale"

    with pytest.raises(BridgeError) as late:
        await service.respond_approval(
            task_id=submitted["task_id"],
            request_id=request["request_id"],
            decision="approve_once",
        )
    assert late.value.code == "REQUEST_STALE"
    await service.close()


@pytest.mark.asyncio
async def test_timeout_keeps_recovery_guard_when_provider_stop_is_unproven(
    tmp_path: Path,
) -> None:
    service = make_service(
        tmp_path,
        adapter=UnknownRecoveryAdapter(),
        limits=BridgeLimits(task_timeout_seconds=1),
    )
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="workspace-write",
        prompt="never-finish",
    )

    timed_out = await wait_for_status(service, submitted["task_id"], "interrupted")
    assert timed_out["error_code"] == "AGENT_TASK_TIMED_OUT"
    guard = service.guard_manager.read(1)
    assert guard is not None
    assert guard.payload["task_id"] == submitted["task_id"]

    lease = service.lease_manager.acquire_exclusive(1)
    lease.release()

    with pytest.raises(BridgeError) as blocked:
        await service.submit_task(
            runtime="fake",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="must-not-start",
        )
    assert blocked.value.code == "WORKDIR_RECOVERY_REQUIRED"
    await service.close()


@pytest.mark.asyncio
async def test_provider_failure_clears_guard_when_reconciliation_proves_stopped(
    tmp_path: Path,
) -> None:
    class FailingFakeAdapter(FakeAdapter):
        async def run_task(self, context: TaskContext) -> AdapterResult:
            raise RuntimeError("provider failed")

    service = make_service(tmp_path, adapter=FailingFakeAdapter())
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="workspace-write",
        prompt="fail",
    )
    failed = await wait_for_status(service, submitted["task_id"], "failed")
    assert failed["error_code"] == "AGENT_PROVIDER_ERROR"
    assert service.guard_manager.read(1) is None
    await service.close()


@pytest.mark.asyncio
async def test_retention_gc_deletes_task_events_requests_and_spooled_result(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        adapter=LargeResultAdapter(),
        limits=BridgeLimits(retention_seconds=1),
    )
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="large:50000",
    )
    await wait_for_status(service, submitted["task_id"], "succeeded")
    spool_path = service.result_spool.results_dir / f"{submitted['task_id']}.txt"
    assert spool_path.is_file()
    assert service.store.count_events(submitted["task_id"]) > 0

    with service.store._connect() as con:
        con.execute(
            "UPDATE tasks SET completed_at = ? WHERE task_id = ?",
            (utc_before(2), submitted["task_id"]),
        )
    assert service._gc_retained() == 1
    assert not spool_path.exists()
    with pytest.raises(BridgeError) as missing:
        service.store.get_task(submitted["task_id"])
    assert missing.value.code == "AGENT_TASK_NOT_FOUND"
    await service.close()


@pytest.mark.asyncio
async def test_restart_reconciliation_never_blindly_reruns_and_keeps_unknown_guard(
    tmp_path: Path,
) -> None:
    adapter = CountingFakeAdapter()
    service = make_service(tmp_path, adapter=adapter)
    task_id = "agt_recovery"
    service.store.create_task(
        task_id=task_id,
        runtime="fake",
        workdir_alias="repo",
        workdir_slot=1,
        relative_cwd="",
        profile="workspace-write",
        continue_from_task_id=None,
        deadline_at=utc_after(60),
        correlation_id="recover-1",
        manifest_json="{}",
        manifest_sha256=hashlib.sha256(b"{}").hexdigest(),
    )
    service.store.transition_task(task_id, TaskStatus.STARTING)
    service.store.transition_task(task_id, TaskStatus.RUNNING)
    service.guard_manager.create(
        slot=1,
        task_id=task_id,
        runtime="fake",
        workdir_alias="repo",
        correlation_id="recover-1",
    )

    assert await service.start() == 1
    task = service.get_task(task_id)
    assert task["status"] == "interrupted"
    assert task["error_code"] == "BRIDGE_RESTARTED"
    assert adapter.runs == 0
    assert service.guard_manager.read(1) is not None

    events = service.read_events(task_id)["events"]
    types = [event["event_type"] for event in events]
    assert "runtime.reconcile_started" in types
    assert "runtime.reconcile_finished" in types
    assert "task.reconciled" in types
    await service.close()


def test_v06_sqlite_schema_migrates_additively_without_fabricating_evidence(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    db_path = state_dir / "state.sqlite3"
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                runtime TEXT NOT NULL,
                workdir_alias TEXT NOT NULL,
                workdir_slot INTEGER NOT NULL,
                relative_cwd TEXT NOT NULL,
                profile TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                continue_from_task_id TEXT,
                native_session_id TEXT,
                native_turn_id TEXT,
                final_response TEXT,
                error_code TEXT,
                error_message TEXT,
                pending_request_id TEXT,
                event_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE pending_requests (
                request_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_json TEXT
            );
            """
        )
        con.execute(
            """
            INSERT INTO tasks (
                task_id, runtime, workdir_alias, workdir_slot, relative_cwd,
                profile, status, created_at, updated_at, completed_at,
                final_response, event_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "agt_legacy",
                "fake",
                "repo",
                1,
                "",
                "review",
                "succeeded",
                "2026-09-22T00:00:00Z",
                "2026-09-22T00:00:01Z",
                "2026-09-22T00:00:01Z",
                "legacy-result",
                0,
            ),
        )
        con.commit()
    finally:
        con.close()

    store = TaskStore(state_dir)
    task = store.get_task("agt_legacy")

    assert task.deadline_at is None
    assert task.correlation_id is None
    assert task.manifest is None
    assert task.manifest_sha256 is None
    assert task.result_storage == "inline"
    assert task.result_size_bytes is None
    assert task.result_sha256 is None

    with store._connect() as migrated:
        task_columns = {row[1] for row in migrated.execute("PRAGMA table_info(tasks)").fetchall()}
        request_columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(pending_requests)").fetchall()
        }
    assert {
        "deadline_at",
        "correlation_id",
        "result_storage",
        "result_size_bytes",
        "result_sha256",
        "manifest_json",
        "manifest_sha256",
    } <= task_columns
    assert "expires_at" in request_columns

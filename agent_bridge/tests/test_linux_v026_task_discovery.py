"""Contract tests for Core-scoped, bounded Agent task discovery."""

from __future__ import annotations

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.models import TaskStatus
from serverfs_agent_bridge.store import TaskStore


def _list(store: TaskStore, **kwargs):
    kwargs.setdefault("policy_revision", "a" * 64)
    return store.list_core_tasks(**kwargs)


def _task(
    store: TaskStore, task_id: str, *, owner: str, alias: str, status: str = "queued"
) -> None:
    store.create_task(
        task_id=task_id,
        runtime="codex",
        workdir_alias=alias,
        workdir_slot=1,
        relative_cwd=".",
        profile="workspace-write",
        continue_from_task_id=None,
        owner_core_id=owner,
    )
    if status != "queued":
        store.transition_task(task_id, "starting")
        if status != "starting":
            store.transition_task(task_id, "running")
        if status not in {"starting", "running"}:
            store.transition_task(task_id, status)


def test_scoped_task_page_projects_only_owned_alias_and_known_fields(tmp_path):
    store = TaskStore(tmp_path)
    _task(store, "agt_a", owner="core-a", alias="Workspace")
    _task(store, "agt_b", owner="core-b", alias="Workspace")
    _task(store, "agt_c", owner="core-a", alias="Other")

    page = _list(store, core_id="core-a", workdir_alias="Workspace", status="active", limit=20)

    assert page["next_cursor"] is None
    assert [task["task_id"] for task in page["tasks"]] == ["agt_a"]
    assert set(page["tasks"][0]) == {
        "task_id",
        "runtime",
        "workdir",
        "status",
        "created_at",
        "updated_at",
        "completed_at",
        "pending_interaction",
        "correlation_id",
    }


def test_task_pages_are_keyset_bounded_and_cursor_scope_bound(tmp_path):
    store = TaskStore(tmp_path)
    for index in range(4):
        _task(store, f"agt_{index}", owner="core-a", alias="Workspace")

    first = _list(store, core_id="core-a", workdir_alias="Workspace", status="active", limit=2)
    assert len(first["tasks"]) == 2
    assert first["next_cursor"]
    second = _list(
        store,
        core_id="core-a",
        workdir_alias="Workspace",
        status="active",
        limit=2,
        cursor=first["next_cursor"],
    )
    assert len(second["tasks"]) == 2
    assert not (
        {task["task_id"] for task in first["tasks"]} & {task["task_id"] for task in second["tasks"]}
    )

    with pytest.raises(BridgeError):
        _list(
            store,
            core_id="core-b",
            workdir_alias="Workspace",
            status="active",
            limit=2,
            cursor=first["next_cursor"],
        )
    with pytest.raises(BridgeError):
        _list(
            store,
            core_id="core-a",
            workdir_alias="Workspace",
            status="active",
            limit=2,
            grant_key_id="rotated-key",
            cursor=first["next_cursor"],
        )
    with pytest.raises(BridgeError):
        _list(
            store,
            core_id="core-a",
            workdir_alias="Workspace",
            status="terminal",
            limit=2,
            cursor=first["next_cursor"],
        )
    with pytest.raises(BridgeError):
        _list(
            store,
            core_id="core-a",
            workdir_alias="Workspace",
            status="active",
            limit=2,
            policy_revision="1" * 64,
            cursor=first["next_cursor"],
        )
    with pytest.raises(BridgeError):
        _list(
            store,
            core_id="core-a",
            workdir_alias="Other",
            status="active",
            limit=2,
            cursor=first["next_cursor"],
        )


@pytest.mark.parametrize("limit", [0, 101, True])
def test_task_discovery_rejects_invalid_limits(tmp_path, limit):
    store = TaskStore(tmp_path)
    with pytest.raises(BridgeError):
        _list(store, core_id="core-a", workdir_alias="Workspace", status="active", limit=limit)


def test_task_discovery_rejects_tampered_cursor(tmp_path):
    store = TaskStore(tmp_path)
    _task(store, "agt_a", owner="core-a", alias="Workspace")
    first = _list(store, core_id="core-a", workdir_alias="Workspace", status="active", limit=1)
    cursor = first["next_cursor"] or "bad"
    with pytest.raises(BridgeError):
        _list(
            store,
            core_id="core-a",
            workdir_alias="Workspace",
            status="active",
            limit=1,
            cursor=cursor + "x",
        )


def test_state_filters_and_equal_timestamp_keyset_ordering(tmp_path):
    store = TaskStore(tmp_path)
    _task(store, "agt_a", owner="core-a", alias="Workspace", status="succeeded")
    for task_id in ("agt_b", "agt_c"):
        _task(store, task_id, owner="core-a", alias="Workspace")
    with store._connect() as con:
        con.execute("UPDATE tasks SET created_at = '2026-01-01T00:00:00Z'")

    active = _list(store, core_id="core-a", workdir_alias="Workspace", status="active", limit=10)
    terminal = _list(
        store, core_id="core-a", workdir_alias="Workspace", status="terminal", limit=10
    )
    all_tasks = _list(store, core_id="core-a", workdir_alias="Workspace", status="all", limit=2)
    assert [task["task_id"] for task in active["tasks"]] == ["agt_c", "agt_b"]
    assert [task["task_id"] for task in terminal["tasks"]] == ["agt_a"]
    assert [task["task_id"] for task in all_tasks["tasks"]] == ["agt_c", "agt_b"]
    assert all_tasks["next_cursor"]


def test_query_plan_uses_alias_order_index_without_owner_global_sort(tmp_path):
    store = TaskStore(tmp_path)
    with store._connect() as con:
        plan = con.execute(
            """EXPLAIN QUERY PLAN
            SELECT tasks.task_id FROM tasks INDEXED BY tasks_alias_cursor_projection
            JOIN core_task_owners AS owner ON owner.task_id = tasks.task_id
            WHERE owner.core_id = ? AND tasks.workdir_alias = ?
              AND tasks.status IN (?, ?, ?, ?, ?)
            ORDER BY tasks.created_at DESC, tasks.task_id DESC LIMIT ?""",
            (
                "core-a",
                "Workspace",
                "queued",
                "starting",
                "running",
                "waiting_for_approval",
                "waiting_for_question",
                101,
            ),
        ).fetchall()
    details = [row["detail"] for row in plan]
    assert any("tasks_alias_cursor_projection" in detail for detail in details)
    assert any("core_task_owners_by_core" in detail for detail in details)
    assert not any("TEMP B-TREE" in detail for detail in details)


@pytest.mark.parametrize(
    ("kind", "waiting_status"),
    [("approval", TaskStatus.WAITING_FOR_APPROVAL), ("question", TaskStatus.WAITING_FOR_QUESTION)],
)
def test_projection_exposes_pending_kind_without_private_payload(tmp_path, kind, waiting_status):
    store = TaskStore(tmp_path)
    _task(store, "agt_pending", owner="core-a", alias="Workspace", status="running")
    store.create_pending_request(
        task_id="agt_pending",
        request_id="req_private",
        kind=kind,
        payload={"credential": "planted-secret-marker", "path": "/private/host/path"},
        waiting_status=waiting_status,
    )
    task = _list(store, core_id="core-a", workdir_alias="Workspace", status="active", limit=1)[
        "tasks"
    ][0]
    assert task["status"] == waiting_status
    assert task["pending_interaction"] == kind
    assert "planted-secret-marker" not in repr(task)
    assert "/private/host/path" not in repr(task)

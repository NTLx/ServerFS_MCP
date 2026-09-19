from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.models import TaskStatus
from serverfs_agent_bridge.store import TaskStore


def make_task(store: TaskStore, task_id: str = "agt_test"):
    return store.create_task(
        task_id=task_id,
        runtime="fake",
        workdir_alias="repo",
        workdir_slot=1,
        relative_cwd="",
        profile="review",
        continue_from_task_id=None,
    )


def test_task_persists_across_store_instances(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    make_task(store)
    store.transition_task("agt_test", TaskStatus.STARTING)
    store.transition_task("agt_test", TaskStatus.RUNNING)

    reopened = TaskStore(tmp_path / "state")
    assert reopened.get_task("agt_test").status == "running"


def test_request_resolution_is_compare_and_set(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    make_task(store)
    store.transition_task("agt_test", TaskStatus.STARTING)
    store.transition_task("agt_test", TaskStatus.RUNNING)
    store.create_pending_request(
        task_id="agt_test",
        request_id="req_test",
        kind="approval",
        payload={"available_decisions": ["approve_once"]},
        waiting_status=TaskStatus.WAITING_FOR_APPROVAL,
    )

    store.resolve_request("req_test", {"decision": "approve_once"})
    assert store.get_task("agt_test").status == "running"

    with pytest.raises(BridgeError) as exc:
        store.resolve_request("req_test", {"decision": "approve_once"})
    assert exc.value.code == "REQUEST_ALREADY_RESOLVED"


def test_concurrent_request_resolution_has_one_winner(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    make_task(store)
    store.transition_task("agt_test", TaskStatus.STARTING)
    store.transition_task("agt_test", TaskStatus.RUNNING)
    store.create_pending_request(
        task_id="agt_test",
        request_id="req_test",
        kind="approval",
        payload={},
        waiting_status=TaskStatus.WAITING_FOR_APPROVAL,
    )

    def resolve() -> str:
        try:
            store.resolve_request("req_test", {"decision": "approve_once"})
        except BridgeError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: resolve(), range(2)))
    assert sorted(results) == ["REQUEST_ALREADY_RESOLVED", "ok"]


def test_restart_interrupts_nonterminal_and_stales_request(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    make_task(store)
    store.transition_task("agt_test", TaskStatus.STARTING)
    store.transition_task("agt_test", TaskStatus.RUNNING)
    store.create_pending_request(
        task_id="agt_test",
        request_id="req_test",
        kind="question",
        payload={"questions": []},
        waiting_status=TaskStatus.WAITING_FOR_QUESTION,
    )

    assert store.interrupt_nonterminal_tasks() == 1
    task = store.get_task("agt_test")
    request = store.get_request("req_test")
    assert task.status == "interrupted"
    assert task.pending_request_id is None
    assert request.status == "stale"


def test_event_cursor(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    make_task(store)
    first = store.append_event("agt_test", "task.started", {})
    second = store.append_event("agt_test", "agent.message", {"text": "done"})
    events = store.list_events("agt_test", after_event_id=first.event_id, limit=10)
    assert [event.event_id for event in events] == [second.event_id]


def test_state_database_and_wal_sidecars_are_private(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = TaskStore(state_dir)
    make_task(store)

    assert state_dir.stat().st_mode & 0o777 == 0o700
    for path in state_dir.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600


def test_existing_non_private_state_dir_fails_without_chmod(tmp_path: Path) -> None:
    state_dir = tmp_path / "public"
    state_dir.mkdir()
    state_dir.chmod(0o755)
    with pytest.raises(ValueError, match="mode 0700"):
        TaskStore(state_dir)
    assert state_dir.stat().st_mode & 0o777 == 0o755


def test_terminal_transition_stales_pending_request_atomically(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    make_task(store)
    store.transition_task("agt_test", TaskStatus.STARTING)
    store.transition_task("agt_test", TaskStatus.RUNNING)
    store.create_pending_request(
        task_id="agt_test",
        request_id="req_test",
        kind="approval",
        payload={},
        waiting_status=TaskStatus.WAITING_FOR_APPROVAL,
    )

    store.transition_task("agt_test", TaskStatus.CANCELLED)

    assert store.get_task("agt_test").pending_request_id is None
    assert store.get_request("req_test").status == "stale"
    with pytest.raises(BridgeError) as exc:
        store.resolve_request("req_test", {"decision": "approve_once"})
    assert exc.value.code == "REQUEST_ALREADY_RESOLVED"


def test_active_task_limit_is_checked_inside_sqlite_transaction(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = TaskStore(state_dir)

    def create(task_id: str) -> str:
        try:
            store.create_task(
                task_id=task_id,
                runtime="fake",
                workdir_alias="repo",
                workdir_slot=1,
                relative_cwd="",
                profile="review",
                continue_from_task_id=None,
                max_active_tasks=1,
            )
        except BridgeError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, ["agt_one", "agt_two"]))

    assert sorted(results) == ["AGENT_TASK_LIMIT_REACHED", "ok"]


def test_event_limit_is_enforced_by_atomic_task_counter(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    make_task(store)
    store.append_event("agt_test", "one", {}, max_events_per_task=2)
    store.append_event("agt_test", "two", {}, max_events_per_task=2)
    with pytest.raises(BridgeError) as exc:
        store.append_event("agt_test", "three", {}, max_events_per_task=2)
    assert exc.value.code == "AGENT_EVENT_LIMIT_REACHED"
    assert len(store.list_events("agt_test", limit=10)) == 2

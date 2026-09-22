from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.adapters.base import AdapterResult
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.service import (
    BridgeLimits,
    BridgeService,
    _redact_embedded_workdir,
)
from serverfs_agent_bridge.store import TaskStore


def make_service(tmp_path: Path) -> BridgeService:
    repo = tmp_path / "repo"
    repo.mkdir()
    policies = PolicyRegistry(
        [
            WorkdirAgentPolicy(
                slot=1,
                alias="repo",
                host_path=repo,
                mode=AgentMode.WORKSPACE_WRITE,
                runtimes=frozenset({"fake"}),
                read_only=False,
            )
        ]
    )
    fake = FakeAdapter()
    return BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=policies,
        adapters={"fake": fake},
        lease_manager=LeaseManager(tmp_path / "locks"),
    )


def make_service_with_limits(tmp_path: Path, limits: BridgeLimits) -> BridgeService:
    service = make_service(tmp_path)
    service.limits = limits
    return service


class FakePreflight:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []
        self.closed = False

    async def evaluate(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("preflight unavailable")
        return {
            "status": "completed",
            "model": "jev-1.13.0",
            "answers": {"single_objective": 0.9},
        }

    async def close(self) -> None:
        self.closed = True


async def wait_for_status(service: BridgeService, task_id: str, *statuses: str) -> dict:
    for _ in range(500):
        task = service.get_task(task_id)
        if task["status"] in statuses:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError(f"task did not reach {statuses}: {service.get_task(task_id)}")


@pytest.mark.asyncio
async def test_submit_returns_before_completion_and_finishes(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="complete:hello",
    )
    assert submitted["status"] == "queued"

    task = await wait_for_status(service, submitted["task_id"], "succeeded")
    assert task["final_response"] == "hello"
    assert service.store.get_task(submitted["task_id"]).native_session_id
    assert "native_session_id" not in task
    await service.close()


@pytest.mark.asyncio
async def test_optional_preflight_is_advisory_and_recorded(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    preflight = FakePreflight()
    service.preflight = preflight
    await service.start()

    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="complete:hello",
    )

    assert submitted["preflight"]["status"] == "completed"
    assert preflight.calls == [
        {
            "runtime": "fake",
            "workdir": "repo",
            "path": "",
            "profile": "review",
            "prompt": "complete:hello",
            "is_continuation": False,
        }
    ]
    events = service.read_events(submitted["task_id"], limit=20)["events"]
    assert any(event["event_type"] == "task.preflight" for event in events)
    task = await wait_for_status(service, submitted["task_id"], "succeeded")
    assert task["final_response"] == "hello"

    await service.close()
    assert preflight.closed is True


@pytest.mark.asyncio
async def test_preflight_failure_fails_open(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.preflight = FakePreflight(fail=True)
    await service.start()

    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="complete:still-runs",
    )

    assert submitted["preflight"] == {"status": "unavailable"}
    task = await wait_for_status(service, submitted["task_id"], "succeeded")
    assert task["final_response"] == "still-runs"
    await service.close()


@pytest.mark.asyncio
async def test_approval_round_trip(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="workspace-write",
        prompt="approval:pytest",
    )
    task = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
    request = task["pending_request"]
    assert request["kind"] == "approval"

    await service.respond_approval(
        task_id=task["task_id"],
        request_id=request["request_id"],
        decision="approve_once",
    )
    finished = await wait_for_status(service, task["task_id"], "succeeded")
    assert finished["final_response"] == "approval=approve_once"
    await service.close()


@pytest.mark.asyncio
async def test_question_round_trip(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="question:Which?",
    )
    task = await wait_for_status(service, submitted["task_id"], "waiting_for_question")
    request = task["pending_request"]

    await service.answer_question(
        task_id=task["task_id"],
        request_id=request["request_id"],
        answers=[
            {
                "question_id": "q1",
                "selected_option_ids": ["b"],
            }
        ],
    )
    finished = await wait_for_status(service, task["task_id"], "succeeded")
    assert '"b"' in finished["final_response"]
    await service.close()


@pytest.mark.asyncio
async def test_workspace_write_busy_is_rejected_at_submit(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start()
    first = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="workspace-write",
        prompt="wait:",
    )
    await wait_for_status(service, first["task_id"], "running")

    with pytest.raises(BridgeError) as exc:
        await service.submit_task(
            runtime="fake",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="complete:second",
        )
    assert exc.value.code == "WORKDIR_BUSY"

    await service.cancel_task(first["task_id"])
    await wait_for_status(service, first["task_id"], "cancelled")
    lease = service.lease_manager.acquire_exclusive(1)
    lease.release()
    await service.close()


@pytest.mark.asyncio
async def test_continuation_reuses_native_session_but_gets_new_task(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start()
    first = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="complete:first",
    )
    await wait_for_status(service, first["task_id"], "succeeded")

    second = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="complete:second",
        continue_from_task_id=first["task_id"],
    )
    second_done = await wait_for_status(service, second["task_id"], "succeeded")
    assert second["task_id"] != first["task_id"]
    first_native_id = service.store.get_task(first["task_id"]).native_session_id
    second_native_id = service.store.get_task(second["task_id"]).native_session_id
    assert second_native_id == first_native_id
    assert "native_session_id" not in second_done
    await service.close()


@pytest.mark.asyncio
async def test_bridge_shutdown_marks_active_task_interrupted(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="wait:",
    )
    await wait_for_status(service, submitted["task_id"], "running")
    await service.close()
    task = service.get_task(submitted["task_id"])
    assert task["status"] == "interrupted"
    assert task["error_code"] == "BRIDGE_SHUTDOWN"


@pytest.mark.asyncio
async def test_cancel_waiting_task_stales_request(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="approval:danger",
    )
    waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
    request_id = waiting["pending_request"]["request_id"]

    await service.cancel_task(submitted["task_id"])
    await wait_for_status(service, submitted["task_id"], "cancelled")
    assert service.store.get_request(request_id).status == "stale"
    assert not service._pending_waiters
    await service.cancel_task(submitted["task_id"])
    await service.close()


@pytest.mark.asyncio
async def test_duplicate_approval_is_rejected_without_second_resolution(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="workspace-write",
        prompt="approval:danger",
    )
    waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
    request_id = waiting["pending_request"]["request_id"]

    await service.respond_approval(
        task_id=submitted["task_id"], request_id=request_id, decision="approve_once"
    )
    with pytest.raises(BridgeError) as exc:
        await service.respond_approval(
            task_id=submitted["task_id"], request_id=request_id, decision="approve_once"
        )
    assert exc.value.code == "REQUEST_ALREADY_RESOLVED"
    await wait_for_status(service, submitted["task_id"], "succeeded")
    await service.close()


@pytest.mark.asyncio
async def test_message_steers_fake_task(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="steer:",
    )
    await wait_for_status(service, submitted["task_id"], "running")
    await service.send_message(task_id=submitted["task_id"], message="focus on tests")
    finished = await wait_for_status(service, submitted["task_id"], "succeeded")
    assert finished["final_response"] == "steered=focus on tests"
    assert service.store.list_events(submitted["task_id"], limit=20)[2].event_type == "user.message"
    await service.close()


@pytest.mark.asyncio
async def test_message_limit_is_enforced_by_service(tmp_path: Path) -> None:
    service = make_service_with_limits(tmp_path, BridgeLimits(max_message_bytes=3))
    await service.start()
    with pytest.raises(BridgeError) as exc:
        await service.send_message(task_id="missing", message="four")
    assert exc.value.code == "AGENT_MESSAGE_TOO_LARGE"
    await service.close()


@pytest.mark.asyncio
async def test_provider_exception_fails_task(tmp_path: Path) -> None:
    class FailingAdapter(FakeAdapter):
        async def run_task(self, context):
            raise RuntimeError("provider failure")

    service = make_service(tmp_path)
    service.adapters = {"fake": FailingAdapter()}
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="workspace-write",
        prompt="complete:never",
    )
    failed = await wait_for_status(service, submitted["task_id"], "failed")
    assert failed["error_code"] == "AGENT_PROVIDER_ERROR"
    lease = service.lease_manager.acquire_exclusive(1)
    lease.release()
    await service.close()


@pytest.mark.asyncio
async def test_close_before_background_start_interrupts_and_releases_lease(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="workspace-write",
        prompt="wait:",
    )
    await service.close()
    assert service.store.get_task(submitted["task_id"]).status == "interrupted"
    lease = service.lease_manager.acquire_exclusive(1)
    lease.release()


@pytest.mark.asyncio
async def test_shutdown_is_interrupted_even_if_adapter_swallows_cancel(
    tmp_path: Path,
) -> None:
    class SwallowCancelAdapter(FakeAdapter):
        async def run_task(self, context):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return AdapterResult(final_response="late")

    service = make_service(tmp_path)
    service.adapters = {"fake": SwallowCancelAdapter()}
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="workspace-write",
        prompt="wait:",
    )
    await wait_for_status(service, submitted["task_id"], "running")
    await service.close()
    assert service.store.get_task(submitted["task_id"]).status == "interrupted"
    lease = service.lease_manager.acquire_exclusive(1)
    lease.release()


@pytest.mark.asyncio
async def test_pending_request_payload_and_final_response_redact_host_root(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    root = service.policies.get("repo").host_path
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt=f"approval:{root}/secret.txt",
    )
    waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
    payload = waiting["pending_request"]["payload"]
    assert str(root) not in str(payload)
    assert payload["command_display"] == "secret.txt"
    await service.respond_approval(
        task_id=submitted["task_id"],
        request_id=waiting["pending_request"]["request_id"],
        decision="approve_once",
    )
    await wait_for_status(service, submitted["task_id"], "succeeded")

    final = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt=f"complete:{root}/result.txt",
    )
    final_task = await wait_for_status(service, final["task_id"], "succeeded")
    assert final_task["final_response"] == "<workdir>/result.txt"
    assert str(root) not in str(final_task)

    sibling = root.parent / f"{root.name}2" / "notes.txt"
    sibling_task = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt=f"complete:{sibling}",
    )
    sibling_done = await wait_for_status(service, sibling_task["task_id"], "succeeded")
    assert sibling_done["final_response"] == "<outside-workdir>"
    assert str(root.parent) not in str(sibling_done)

    prose = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt=f"complete:failed at {root}/trace.log",
    )
    prose_done = await wait_for_status(service, prose["task_id"], "succeeded")
    assert prose_done["final_response"] == "failed at <workdir>/trace.log"
    assert str(root) not in str(prose_done)

    sibling_prose = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt=f"complete:see {sibling} for details",
    )
    sibling_prose_done = await wait_for_status(service, sibling_prose["task_id"], "succeeded")
    assert sibling_prose_done["final_response"] == "see <outside-workdir> for details"
    assert str(root.parent) not in str(sibling_prose_done)

    service._append_event(
        final["task_id"],
        "provider.path",
        {
            "cwd": str(root),
            "changes": [
                {"path": str(root / "inside.txt")},
                {"path": str(root.parent / "outside.txt")},
            ],
        },
    )
    events = service.read_events(final["task_id"])["events"]
    provider_event = next(event for event in events if event["event_type"] == "provider.path")
    assert provider_event["payload"]["cwd"] == "."
    assert provider_event["payload"]["changes"] == [
        {"path": "inside.txt"},
        {"path": "<outside-workdir>"},
    ]
    assert str(root) not in str(provider_event)
    await service.close()


@pytest.mark.asyncio
async def test_provider_bridge_error_message_redacts_host_paths(tmp_path: Path) -> None:
    class ErrorAdapter(FakeAdapter):
        async def run_task(self, context):
            raise BridgeError(
                "AGENT_PROVIDER_ERROR",
                f"provider failed at {context.workdir_root}/secret.txt",
            )

    service = make_service(tmp_path)
    root = service.policies.get("repo").host_path
    service.adapters = {"fake": ErrorAdapter()}
    await service.start()
    submitted = await service.submit_task(
        runtime="fake",
        workdir="repo",
        path="",
        profile="review",
        prompt="fail",
    )
    failed = await wait_for_status(service, submitted["task_id"], "failed")
    assert failed["error_code"] == "AGENT_PROVIDER_ERROR"
    assert failed["error_message"] == "provider failed at <workdir>/secret.txt"
    assert str(root) not in str(failed)
    await service.close()


def test_embedded_workdir_redaction_token_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    sibling = tmp_path / "repo2"
    sibling.mkdir()

    def redact(value: str) -> str:
        return _redact_embedded_workdir(root, value)

    # Whole-token workdir references keep the display form.
    assert redact(f"failed at {root}/trace.log") == "failed at <workdir>/trace.log"
    assert redact(f"failed at {root}") == "failed at <workdir>"

    # A sibling directory is a different path and must not leak the host root.
    assert redact(f"see {sibling} for details") == "see <outside-workdir> for details"
    assert redact(f"see '{sibling}/a.txt' now") == "see '<outside-workdir>' now"

    # Several tokens in one string are resolved independently.
    assert (
        redact(f"first {root}/a.txt then {sibling}/b.txt")
        == "first <workdir>/a.txt then <outside-workdir>"
    )

    # Punctuation abutting a path is prose and survives redaction.
    assert redact(f"see {sibling}/notes.txt.") == "see <outside-workdir>."
    assert redact(f"see {sibling}/notes.txt! ok") == "see <outside-workdir>! ok"

    # Text without a workdir reference is returned unchanged.
    assert redact("nothing to redact here") == "nothing to redact here"

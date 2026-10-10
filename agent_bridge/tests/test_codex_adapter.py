from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from codex_mock_provider import MockCodexServer
from platform_contract import WINDOWS, linux_only
from serverfs_agent_bridge.adapters.base import ReconcileResult
from serverfs_agent_bridge.adapters.codex import CodexAdapter
from serverfs_agent_bridge.adapters.codex_transport import CodexEndpoint
from serverfs_agent_bridge.config import CodexSettings
from serverfs_agent_bridge.lease_identity import slot_lease_id
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode, ReconciliationStatus
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore


@pytest.fixture
def codex_home() -> Iterator[Path]:
    """A Codex home short enough to bind the production control-socket layout.

    ``CodexSettings.control_socket`` nests two directories below the codex home,
    and AF_UNIX paths are capped near 107 bytes.  A pytest ``tmp_path`` plus that
    layout overflows the limit, so the mock daemon binds under a short root.
    """
    root = Path(tempfile.mkdtemp(prefix="sfs-codex-", dir=tempfile.gettempdir()))
    home = root / "codex-home"
    home.mkdir()
    try:
        yield home
    finally:
        shutil.rmtree(root, ignore_errors=True)


def make_service(
    tmp_path: Path,
    codex_home: Path,
    *,
    endpoint: CodexEndpoint | None = None,
    event_idle_timeout_seconds: float | None = 2,
) -> BridgeService:
    """A BridgeService whose Codex adapter talks to the double over the endpoint it is serving.

    ``endpoint=None`` is the Linux managed-daemon control socket, resolved from
    ``CodexSettings.control_socket`` exactly as production does. A loopback endpoint is what the
    Windows runtime uses instead. The lease and state directories go through this platform's own
    implementations either way, so a Windows run exercises the real writer lease rather than a
    stand-in -- which is why this no longer carries the Linux gate the module once needed.
    """
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    # A native Windows deployment carries no slots (§5.3), so the lease is keyed by alias there and
    # pre-created by the Bridge. Linux keeps the slot layout. Using the platform's real keying is
    # what lets this fixture exercise the Windows writer lease instead of skipping around it.
    policy = WorkdirAgentPolicy(
        slot=None if WINDOWS else 1,
        alias="repo",
        host_path=repo,
        mode=AgentMode.WORKSPACE_WRITE,
        runtimes=frozenset({"codex"}),
        read_only=False,
    )
    adapter = CodexAdapter(
        CodexSettings(
            enabled=True,
            autostart=False,
            codex_home=codex_home,
            request_timeout_seconds=2,
            event_idle_timeout_seconds=event_idle_timeout_seconds,
        ),
        state_dir=tmp_path / "state",
    )
    if endpoint is not None:
        # The seam lives here rather than in the product: substituting the runtime object keeps the
        # operator surface free of a fixture-only injection parameter, and it still drives the real
        # ``_acquire_connection`` path that a Windows run takes.
        adapter._windows_runtime = _FixedEndpointRuntime(endpoint)
    return BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=PolicyRegistry([policy]),
        adapters={"codex": adapter},
        lease_manager=LeaseManager(tmp_path / "locks", lease_ids=[policy.lease_id]),
    )


class _FixedEndpointRuntime:
    """Stands in for the Bridge-owned app-server, handing back an endpoint the test controls.

    It implements only the two members the adapter calls, so it is not a second implementation of
    the lifecycle: single-flight startup, token handling and readiness are covered against the real
    object in test_codex_windows_runtime.py.
    """

    def __init__(self, endpoint: CodexEndpoint) -> None:
        self._endpoint = endpoint
        self.ensure_started_calls = 0
        self.closed = False

    async def ensure_started(self) -> CodexEndpoint:
        self.ensure_started_calls += 1
        return self._endpoint

    async def close(self) -> None:
        self.closed = True


async def wait_for_status(service: BridgeService, task_id: str, *statuses: str) -> dict[str, Any]:
    for _ in range(300):
        task = service.get_task(task_id)
        if task["status"] in statuses:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError(f"task did not reach {statuses}: {service.get_task(task_id)}")


@pytest.mark.asyncio
@linux_only(
    "the managed daemon is the Linux transport: on Windows the Bridge owns the app-server child "
    "instead, so probe has to start it in order to answer at all. The Windows counterpart is "
    "test_codex_windows_runtime.py::test_probe_starts_the_bridge_owned_app_server."
)
async def test_codex_probe_never_autostarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    adapter = CodexAdapter(
        CodexSettings(
            enabled=True,
            autostart=True,
            codex_home=codex_home,
            request_timeout_seconds=0.1,
        ),
        state_dir=tmp_path / "state",
    )
    called = False

    async def forbidden_start() -> None:
        nonlocal called
        called = True
        raise AssertionError("probe must not autostart Codex")

    monkeypatch.setattr(adapter, "_start_official_daemon", forbidden_start)
    info = await adapter.probe()
    assert info.available is False
    assert called is False


@pytest.mark.asyncio
async def test_codex_normal_task_and_continuation(tmp_path: Path, codex_home: Path) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        first = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="first",
        )
        first_done = await wait_for_status(service, first["task_id"], "succeeded")
        assert first_done["final_response"] == "done:first"

        second = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="second",
            continue_from_task_id=first["task_id"],
        )
        second_done = await wait_for_status(service, second["task_id"], "succeeded")
        assert second_done["final_response"] == "done:second"
        assert mock.thread_starts == 1
        assert mock.thread_resumes == ["thread-1"]
        assert all(
            set(params)
            == {"threadId", "input", "cwd", "approvalPolicy", "approvalsReviewer"}
            and params["approvalPolicy"] == "on-request"
            and params["approvalsReviewer"] == "user"
            for params in mock.turn_starts
        )
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_model_discovery_and_request_scoped_override(
    tmp_path: Path, codex_home: Path
) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        catalog = await service.list_models(runtime="codex")
        assert catalog["status"] == "ok"
        assert catalog["scope"] == "runtime_catalog"
        assert [item["id"] for item in catalog["models"]] == [
            "gpt-5.6-codex",
            "gpt-5.6-mini",
        ]
        assert catalog["models"][0]["is_default"] is True
        assert catalog["models"][0]["reasoning"]["default"] == "medium"

        first = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="selected",
            model="gpt-5.6-codex",
        )
        await wait_for_status(service, first["task_id"], "succeeded")
        assert mock.thread_start_params[-1]["model"] == "gpt-5.6-codex"

        second = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="continued",
            model="gpt-5.6-mini",
            continue_from_task_id=first["task_id"],
        )
        await wait_for_status(service, second["task_id"], "succeeded")
        assert mock.thread_resume_params[-1]["model"] == "gpt-5.6-mini"
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
@linux_only(
    "the failure under test is the absent Linux managed daemon, identified by its exact message. "
    "The Windows runtime fails differently -- it starts its own app-server -- and its restart "
    "recovery is covered in test_codex_windows_runtime.py."
)
async def test_reconcile_clears_guard_after_control_socket_failure_before_thread_start(
    tmp_path: Path,
    codex_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No double here on purpose: the case is a transport that failed before any thread started, so
    # the adapter must classify it from the recorded failure rather than from a live endpoint.
    service = make_service(tmp_path, codex_home)
    adapter = service.adapters["codex"]
    reconcile = adapter.reconcile_task

    async def available_probe():
        return adapter._runtime_info(available=True, version="test")

    async def legacy_reconcile(_task):
        return ReconcileResult(
            status=ReconciliationStatus.NOT_RECOVERABLE,
            provider_active=None,
            detail="task has no persisted Codex thread id",
        )

    monkeypatch.setattr(adapter, "probe", available_probe)
    monkeypatch.setattr(adapter, "reconcile_task", legacy_reconcile)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="socket-unavailable-before-thread-start",
        )
        failed = await wait_for_status(service, submitted["task_id"], "failed")
        assert failed["error_code"] == "AGENT_RUNTIME_NOT_READY"
        assert failed["error_message"] == ("Codex App Server daemon control socket is unavailable")

        task = service.store.get_task(submitted["task_id"])
        assert task.native_session_id is None
        assert task.native_turn_id is None
        assert service.guard_manager.read(slot_lease_id(1)) is not None

        ambiguous = replace(task, error_message="official Codex daemon start failed")
        monkeypatch.setattr(adapter, "reconcile_task", reconcile)
        unresolved = await adapter.reconcile_task(ambiguous)
        assert unresolved.provider_active is None

        result = await service._reconcile_guard(slot_lease_id(1))
        assert result is not None
        assert result.provider_active is False
        assert service.guard_manager.read(slot_lease_id(1)) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_codex_command_approval_round_trip(tmp_path: Path, codex_home: Path) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="approval",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
        pending = waiting["pending_request"]
        assert pending["payload"]["category"] == "command"
        assert pending["payload"]["command_display"] == "pytest -q"

        await service.respond_approval(
            task_id=submitted["task_id"],
            request_id=pending["request_id"],
            decision="approve_once",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "approval:accept"
        assert mock.native_responses["native-approval"]["result"] == {"decision": "accept"}
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_network_approval_round_trip_preserves_native_policy(
    tmp_path: Path, codex_home: Path
) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="unsafe-network",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
        pending = waiting["pending_request"]
        assert pending["payload"]["category"] == "command"
        assert pending["payload"]["network_approval_context"]["host"] == "example.com"

        await service.respond_approval(
            task_id=submitted["task_id"],
            request_id=pending["request_id"],
            decision="approve_once",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "approval:accept"
        assert mock.native_responses["native-network"]["result"] == {"decision": "accept"}
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_file_approval_inside_workdir_round_trip(
    tmp_path: Path, codex_home: Path
) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="file-approval",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
        pending = waiting["pending_request"]
        assert pending["payload"]["category"] == "file_change"
        assert pending["payload"]["file_changes"][0]["path"] == "."

        await service.respond_approval(
            task_id=submitted["task_id"],
            request_id=pending["request_id"],
            decision="approve_once",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "approval:accept"
        assert mock.native_responses["native-file"]["result"] == {"decision": "accept"}
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_file_approval_outside_workdir_is_still_user_decided(
    tmp_path: Path, codex_home: Path
) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="file-outside",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
        pending = waiting["pending_request"]
        assert pending["payload"]["category"] == "file_change"
        assert pending["payload"]["file_changes"][0]["path"] == "<outside-workdir>"

        await service.respond_approval(
            task_id=submitted["task_id"],
            request_id=pending["request_id"],
            decision="approve_once",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "approval:accept"
        assert mock.native_responses["native-file-outside"]["result"] == {"decision": "accept"}
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_permission_request_round_trip(tmp_path: Path, codex_home: Path) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="permission",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
        pending = waiting["pending_request"]
        assert pending["payload"]["category"] == "provider_permission"
        permission = pending["payload"]["requested_permissions"][0]
        permission_id = permission["permission_id"]
        assert permission_id == "fileSystem"
        details = permission["details"]
        assert details["read"] == ["generated", "<outside-workdir>"]
        assert details["write"] == ["generated"]

        await service.respond_approval(
            task_id=submitted["task_id"],
            request_id=pending["request_id"],
            decision="approve_session",
            granted_permission_ids=[permission_id],
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "permission:session:True"
        native = mock.native_responses["native-permission"]["result"]
        assert native["scope"] == "session"
        native_fs = native["permissions"]["fileSystem"]
        # Compared by path component, not by separator: the intent is "the granted root is the
        # workdir's own generated directory", and a literal "/generated" would fail on Windows for a
        # reason that has nothing to do with the permission contract under test.
        assert Path(native_fs["write"][0]).name == "generated"
        assert Path(native_fs["read"][0]).name == "generated"
        assert Path(native_fs["read"][1]).name == "outside-generated"
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_unsupported_mcp_elicitation_fails_promptly(
    tmp_path: Path, codex_home: Path
) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="mcp-elicitation",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "request-error:-32601"
        assert done["pending_request_id"] is None
        assert mock.native_responses["native-mcp-elicitation"]["error"]["code"] == -32601
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_malformed_server_request_gets_error_response(
    tmp_path: Path, codex_home: Path
) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="malformed-question",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "request-error:-32000"
        assert done["pending_request_id"] is None
        assert mock.native_responses["native-malformed"]["error"]["code"] == -32000
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_question_round_trip(tmp_path: Path, codex_home: Path) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="question",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_question")
        pending = waiting["pending_request"]
        option_id = pending["payload"]["questions"][0]["options"][1]["option_id"]
        await service.answer_question(
            task_id=submitted["task_id"],
            request_id=pending["request_id"],
            answers=[
                {
                    "question_id": "q1",
                    "selected_option_ids": [option_id],
                    "text": "details",
                }
            ],
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "question:B,details"
        assert mock.native_responses["native-question"]["result"]["answers"]["q1"]["answers"] == [
            "B",
            "details",
        ]
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_steer_and_cancel(tmp_path: Path, codex_home: Path) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        steer = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="steer",
        )
        await wait_for_status(service, steer["task_id"], "running")
        await service.send_message(task_id=steer["task_id"], message="focus API")
        steer_done = await wait_for_status(service, steer["task_id"], "succeeded")
        assert steer_done["final_response"] == "steered:focus API"
        assert mock.steers == ["focus API"]

        waiting = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="wait",
        )
        await wait_for_status(service, waiting["task_id"], "running")
        await service.cancel_task(waiting["task_id"])
        await wait_for_status(service, waiting["task_id"], "cancelled")
        assert mock.interrupts == 1
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_native_request_auto_resolution_stales_bridge_request(
    tmp_path: Path,
    codex_home: Path,
) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="auto-resolve",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
        request_id = waiting["pending_request"]["request_id"]
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "auto-done"
        assert done["pending_request_id"] is None
        assert service.store.get_request(request_id).status == "stale"
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_review_profile_is_rejected_in_native_mode(
    tmp_path: Path, codex_home: Path
) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="review",
            prompt="first",
        )
        failed = await wait_for_status(service, submitted["task_id"], "failed")
        assert failed["error_code"] == "AGENT_PROFILE_NOT_ALLOWED"
        assert mock.thread_starts == 0
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_numeric_request_id_survives_response_round_trip(
    tmp_path: Path, codex_home: Path
) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home, endpoint=mock.endpoint)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="integer-id",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
        await service.respond_approval(
            task_id=submitted["task_id"],
            request_id=waiting["pending_request"]["request_id"],
            decision="approve_once",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "approval:accept"
        native = mock.native_responses["4242"]
        assert native["result"] == {"decision": "accept"}
        # Codex correlates by the id it sent; a numeric id must not come back
        # as a string, or the daemon cannot match the response to its request.
        assert native["id"] == 4242
        assert isinstance(native["id"], int)
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_idle_timeout_does_not_fail_a_turn_waiting_for_a_human(
    tmp_path: Path, codex_home: Path
) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    # The idle timeout is far shorter than the time the user takes to answer.
    service = make_service(
        tmp_path, codex_home, endpoint=mock.endpoint, event_idle_timeout_seconds=0.2
    )
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="codex",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="approval",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
        await asyncio.sleep(0.5)
        # Codex is silent because it is blocked on the human, not stalled.
        assert service.get_task(submitted["task_id"])["status"] == "waiting_for_approval"

        await service.respond_approval(
            task_id=submitted["task_id"],
            request_id=waiting["pending_request"]["request_id"],
            decision="approve_once",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "approval:accept"
    finally:
        await service.close()
        await mock.close()

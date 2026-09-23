from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import unix_serve

from serverfs_agent_bridge.adapters.base import ReconcileResult
from serverfs_agent_bridge.adapters.codex import CodexAdapter
from serverfs_agent_bridge.config import CodexSettings
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


class MockCodexServer:
    def __init__(self, codex_home: Path) -> None:
        self.socket_path = codex_home / "app-server-control" / "app-server-control.sock"
        self.socket_path.parent.mkdir(parents=True)
        self.server = None
        self.thread_starts = 0
        self.thread_resumes: list[str] = []
        self.steers: list[str] = []
        self.interrupts = 0
        self.turn_starts: list[dict[str, Any]] = []
        self.native_responses: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        self.server = await unix_serve(self._handler, path=str(self.socket_path))

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handler(self, ws) -> None:
        initialize = json.loads(await ws.recv())
        assert initialize["method"] == "initialize"
        await _send(
            ws,
            {
                "jsonrpc": "2.0",
                "id": initialize["id"],
                "result": {
                    "userAgent": "codex-app-server/0.153.4",
                    "codexHome": str(self.socket_path.parents[1]),
                },
            },
        )
        initialized = json.loads(await ws.recv())
        assert initialized["method"] == "initialized"

        thread_id = "thread-1"
        turn_id = "turn-1"
        while True:
            try:
                message = json.loads(await ws.recv())
            except Exception:
                return

            method = message.get("method")
            if method == "thread/start":
                self.thread_starts += 1
                assert set(message["params"]) == {"cwd", "serviceName"}
                assert "sandbox" not in message["params"]
                assert "approvalPolicy" not in message["params"]
                assert "config" not in message["params"]
                await _respond(ws, message, {"thread": {"id": thread_id}})
                continue
            if method == "thread/resume":
                assert set(message["params"]) == {"threadId", "cwd"}
                assert "sandbox" not in message["params"]
                assert "approvalPolicy" not in message["params"]
                assert "config" not in message["params"]
                thread_id = message["params"]["threadId"]
                self.thread_resumes.append(thread_id)
                await _respond(ws, message, {"thread": {"id": thread_id}})
                continue
            if method == "turn/start":
                params = message["params"]
                self.turn_starts.append(params)
                assert set(params) == {"threadId", "input", "cwd"}
                assert "sandboxPolicy" not in params
                assert "approvalPolicy" not in params
                prompt = params["input"][0]["text"]
                await _respond(
                    ws,
                    message,
                    {"turn": {"id": turn_id, "status": "inProgress", "items": []}},
                )
                if prompt == "approval":
                    await _send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-approval",
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "command": ["pytest", "-q"],
                                "cwd": message["params"]["cwd"],
                                "reason": "Run tests",
                                "availableDecisions": [
                                    "accept",
                                    "acceptForSession",
                                    "decline",
                                    "cancel",
                                ],
                            },
                        },
                    )
                elif prompt == "unsafe-network":
                    await _send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-network",
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "command": ["curl", "https://example.com"],
                                "cwd": message["params"]["cwd"],
                                "networkApprovalContext": {
                                    "host": "example.com",
                                    "protocol": "https",
                                },
                                "availableDecisions": ["accept", "decline", "cancel"],
                            },
                        },
                    )
                elif prompt == "file-approval":
                    await _send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-file",
                            "method": "item/fileChange/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "reason": "Apply patch",
                                "grantRoot": message["params"]["cwd"],
                            },
                        },
                    )
                elif prompt == "file-outside":
                    await _send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-file-outside",
                            "method": "item/fileChange/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "reason": "Outside write",
                                "grantRoot": str(Path(message["params"]["cwd"]).parent),
                            },
                        },
                    )
                elif prompt == "permission":
                    cwd = Path(message["params"]["cwd"])
                    inside = str(cwd / "generated")
                    outside = str(cwd.parent / "outside-generated")
                    await _send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-permission",
                            "method": "item/permissions/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "reason": "Need generated output",
                                "cwd": message["params"]["cwd"],
                                "permissions": {
                                    "fileSystem": {
                                        "read": [inside, outside],
                                        "write": [inside],
                                    }
                                },
                            },
                        },
                    )
                elif prompt == "mcp-elicitation":
                    await _send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-mcp-elicitation",
                            "method": "mcpServer/elicitation/request",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "serverName": "example-mcp",
                                "mode": "form",
                                "message": "Need additional input",
                                "requestedSchema": {
                                    "type": "object",
                                    "properties": {"value": {"type": "string"}},
                                    "required": ["value"],
                                },
                            },
                        },
                    )
                elif prompt == "malformed-question":
                    await _send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-malformed",
                            "method": "item/tool/requestUserInput",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "questions": [42],
                            },
                        },
                    )
                elif prompt == "question":
                    await _send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-question",
                            "method": "item/tool/requestUserInput",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "itemId": "item-q",
                                "questions": [
                                    {
                                        "id": "q1",
                                        "header": "Choice",
                                        "question": "Which option?",
                                        "isOther": True,
                                        "isSecret": False,
                                        "options": [
                                            {"label": "A", "description": "first"},
                                            {"label": "B", "description": "second"},
                                        ],
                                    }
                                ],
                                "isBlocking": True,
                                "autoResolutionMs": None,
                            },
                        },
                    )
                elif prompt == "auto-resolve":
                    await _send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-auto",
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "command": "echo wait",
                                "availableDecisions": ["accept", "decline", "cancel"],
                            },
                        },
                    )
                    await asyncio.sleep(0.05)
                    await _notify_resolved(ws, thread_id, "native-auto")
                    await _complete(ws, thread_id, turn_id, "auto-done")
                elif prompt == "integer-id":
                    # The official protocol types a request id as `string | int64`,
                    # so a numeric id must survive the response round trip.
                    await _send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": 4242,
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "command": "echo int",
                                "availableDecisions": ["accept", "decline"],
                            },
                        },
                    )
                elif prompt == "steer":
                    await _send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/started",
                            "params": {
                                "threadId": thread_id,
                                "turn": {"id": turn_id, "status": "inProgress", "items": []},
                            },
                        },
                    )
                elif prompt == "wait":
                    pass
                else:
                    await _complete(ws, thread_id, turn_id, f"done:{prompt}")
                continue
            if method == "turn/steer":
                text = message["params"]["input"][0]["text"]
                self.steers.append(text)
                await _respond(ws, message, {"turnId": turn_id})
                await _complete(ws, thread_id, turn_id, f"steered:{text}")
                continue
            if method == "turn/interrupt":
                self.interrupts += 1
                await _respond(ws, message, {})
                await _send(
                    ws,
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {
                                "id": turn_id,
                                "status": "interrupted",
                                "items": [],
                            },
                        },
                    },
                )
                continue

            if "id" in message and "method" not in message:
                request_id = str(message["id"])
                self.native_responses[request_id] = message
                await _notify_resolved(ws, thread_id, request_id)
                if "error" in message:
                    code = message["error"].get("code")
                    await _complete(ws, thread_id, turn_id, f"request-error:{code}")
                elif request_id == "native-question":
                    answer = message["result"]["answers"]["q1"]["answers"]
                    await _complete(
                        ws,
                        thread_id,
                        turn_id,
                        "question:" + ",".join(answer),
                    )
                elif request_id == "native-permission":
                    permissions = message["result"].get("permissions", {})
                    scope = message["result"].get("scope", "turn")
                    await _complete(
                        ws,
                        thread_id,
                        turn_id,
                        f"permission:{scope}:{bool(permissions)}",
                    )
                else:
                    decision = message["result"].get("decision", "unknown")
                    await _complete(ws, thread_id, turn_id, f"approval:{decision}")


async def _send(ws, payload: dict[str, Any]) -> None:
    await ws.send(json.dumps(payload))


async def _respond(ws, request: dict[str, Any], result: dict[str, Any]) -> None:
    await _send(ws, {"jsonrpc": "2.0", "id": request["id"], "result": result})


async def _notify_resolved(ws, thread_id: str, request_id: str) -> None:
    await _send(
        ws,
        {
            "jsonrpc": "2.0",
            "method": "serverRequest/resolved",
            "params": {"threadId": thread_id, "requestId": request_id},
        },
    )


async def _complete(ws, thread_id: str, turn_id: str, text: str) -> None:
    await _send(
        ws,
        {
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "type": "agentMessage",
                    "text": text,
                    "phase": "final_answer",
                },
            },
        },
    )
    await _send(
        ws,
        {
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {
                    "id": turn_id,
                    "status": "completed",
                    "items": [],
                },
            },
        },
    )


def make_service(
    tmp_path: Path,
    codex_home: Path,
    *,
    event_idle_timeout_seconds: float | None = 2,
) -> BridgeService:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    adapter = CodexAdapter(
        CodexSettings(
            enabled=True,
            autostart=False,
            codex_home=codex_home,
            request_timeout_seconds=2,
            event_idle_timeout_seconds=event_idle_timeout_seconds,
        )
    )
    return BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=PolicyRegistry(
            [
                WorkdirAgentPolicy(
                    slot=1,
                    alias="repo",
                    host_path=repo,
                    mode=AgentMode.WORKSPACE_WRITE,
                    runtimes=frozenset({"codex"}),
                    read_only=False,
                )
            ]
        ),
        adapters={"codex": adapter},
        lease_manager=LeaseManager(tmp_path / "locks"),
    )


async def wait_for_status(service: BridgeService, task_id: str, *statuses: str) -> dict[str, Any]:
    for _ in range(300):
        task = service.get_task(task_id)
        if task["status"] in statuses:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError(f"task did not reach {statuses}: {service.get_task(task_id)}")


@pytest.mark.asyncio
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
        )
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
    service = make_service(tmp_path, codex_home)
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
        assert all(set(params) == {"threadId", "input", "cwd"} for params in mock.turn_starts)
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_reconcile_clears_guard_after_control_socket_failure_before_thread_start(
    tmp_path: Path,
    codex_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        assert service.guard_manager.read(1) is not None

        ambiguous = replace(task, error_message="official Codex daemon start failed")
        monkeypatch.setattr(adapter, "reconcile_task", reconcile)
        unresolved = await adapter.reconcile_task(ambiguous)
        assert unresolved.provider_active is None

        result = await service._reconcile_guard(1)
        assert result is not None
        assert result.provider_active is False
        assert service.guard_manager.read(1) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_codex_command_approval_round_trip(tmp_path: Path, codex_home: Path) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home)
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
    service = make_service(tmp_path, codex_home)
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
    service = make_service(tmp_path, codex_home)
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
    service = make_service(tmp_path, codex_home)
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
    service = make_service(tmp_path, codex_home)
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
        assert native_fs["write"][0].endswith("/generated")
        assert native_fs["read"][0].endswith("/generated")
        assert native_fs["read"][1].endswith("/outside-generated")
    finally:
        await service.close()
        await mock.close()


@pytest.mark.asyncio
async def test_codex_unsupported_mcp_elicitation_fails_promptly(
    tmp_path: Path, codex_home: Path
) -> None:
    mock = MockCodexServer(codex_home)
    await mock.start()
    service = make_service(tmp_path, codex_home)
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
    service = make_service(tmp_path, codex_home)
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
    service = make_service(tmp_path, codex_home)
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
    service = make_service(tmp_path, codex_home)
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
    service = make_service(tmp_path, codex_home)
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
    service = make_service(tmp_path, codex_home)
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
    service = make_service(tmp_path, codex_home)
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
    service = make_service(tmp_path, codex_home, event_idle_timeout_seconds=0.2)
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

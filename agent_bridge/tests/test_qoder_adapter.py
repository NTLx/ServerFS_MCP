from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import serverfs_agent_bridge.adapters.qoder as qoder_module
from platform_contract import linux_only
from serverfs_agent_bridge.adapters.qoder import QoderAdapter
from serverfs_agent_bridge.config import QoderSettings
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore

pytestmark = linux_only(
    "the Qoder test service stores tasks in UID/mode private state under a flock lease"
)


@dataclass
class FakeTextBlock:
    text: str


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class FakeAssistantMessage:
    content: list[Any]
    session_id: str | None = None


@dataclass
class FakeSystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class FakeResultMessage:
    result: str | None
    session_id: str
    is_error: bool = False
    subtype: str = "success"
    uuid: str | None = None
    errors: list[str] | None = None


@dataclass
class FakePermissionContext:
    suggestions: list[Any] | None = None
    blocked_path: str | None = None
    decision_reason: str | None = None
    title: str | None = None
    display_name: str | None = None
    description: str | None = None


class FakeQoderClient:
    def __init__(self, options: Any) -> None:
        self.options = options
        self.prompt: str | None = None
        self.connected = False
        self.interrupted = False

    async def connect(self, prompt: str | None = None) -> None:
        self.prompt = prompt
        self.connected = True

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.connected = False

    async def get_available_models(self) -> list[dict[str, Any]]:
        return [
            {
                "value": "qfmodel",
                "displayName": "Qwen3.8-Flash",
                "description": "Fast model",
                "isEnabled": True,
                "isFree": True,
                "priceFactor": 0,
            },
            {
                "value": "qmodel_38max",
                "displayName": "Qwen3.8-Max",
                "description": "Deep model",
                "isEnabled": True,
                "isFree": False,
                "priceFactor": 1,
            },
        ]

    async def receive_response(self):
        assert self.prompt is not None
        session_id = self.options.resume or "qoder-session-1"
        yield FakeSystemMessage("init", {"session_id": session_id, "model": "test-model"})

        if self.prompt == "approval":
            context = FakePermissionContext(
                suggestions=[
                    {
                        "type": "addRules",
                        "destination": "session",
                        "rules": [{"tool": "Bash"}],
                    }
                ],
                title="Run command",
                description="Qoder wants to run a command",
            )
            result = await self.options.can_use_tool(
                "Bash",
                {"command": "echo hello"},
                context,
            )
            scope = "session" if getattr(result, "updated_permissions", None) else "once"
            yield FakeResultMessage(
                result=f"approval:{result.behavior}:{scope}",
                session_id=session_id,
            )
            return

        if self.prompt == "approval-persistent":
            context = FakePermissionContext(
                suggestions=[
                    {
                        "type": "addRules",
                        "destination": "user",
                        "rules": [{"tool": "Bash"}],
                    }
                ],
                title="Persist permission",
            )
            result = await self.options.can_use_tool(
                "Bash",
                {"command": "echo persistent"},
                context,
            )
            yield FakeResultMessage(
                result=f"persistent:{result.behavior}",
                session_id=session_id,
            )
            return

        if self.prompt == "question":
            context = FakePermissionContext(title="Ask question")
            result = await self.options.can_use_tool(
                "AskUserQuestion",
                {
                    "questions": [
                        {
                            "question": "Which option?",
                            "header": "Choice",
                            "multiSelect": False,
                            "options": [
                                {
                                    "label": "A",
                                    "description": "First",
                                    "preview": "Preview A",
                                },
                                {"label": "B", "description": "Second"},
                            ],
                        }
                    ]
                },
                context,
            )
            updated_input = getattr(result, "updated_input", {}) or {}
            answers = updated_input.get("answers", {})
            yield FakeResultMessage(
                result=f"question:{answers.get('Which option?')}",
                session_id=session_id,
            )
            return

        if self.prompt == "question-duplicate":
            context = FakePermissionContext(title="Ask duplicate")
            result = await self.options.can_use_tool(
                "AskUserQuestion",
                {
                    "questions": [
                        {"question": "Same text?", "options": [{"label": "A"}]},
                        {"question": "Same text?", "options": [{"label": "B"}]},
                    ]
                },
                context,
            )
            yield FakeResultMessage(
                result=f"duplicate:{result.behavior}",
                session_id=session_id,
            )
            return

        if self.prompt == "wait":
            while not self.interrupted:
                await asyncio.sleep(0.01)
            yield FakeResultMessage(result="interrupted", session_id=session_id)
            return

        yield FakeAssistantMessage([FakeTextBlock(self.prompt)], session_id=session_id)
        yield FakeResultMessage(
            result=self.prompt,
            session_id=session_id,
            uuid="qoder-result-uuid",
        )


class FakeClientFactory:
    def __init__(self) -> None:
        self.options: list[Any] = []
        self.clients: list[FakeQoderClient] = []

    def __call__(self, options: Any) -> FakeQoderClient:
        self.options.append(options)
        client = FakeQoderClient(options)
        self.clients.append(client)
        return client


def make_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    event_idle_timeout_seconds: float | None = None,
) -> tuple[BridgeService, FakeClientFactory]:
    monkeypatch.setattr(qoder_module, "AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr(qoder_module, "TextBlock", FakeTextBlock)
    monkeypatch.setattr(qoder_module, "ToolUseBlock", FakeToolUseBlock)
    monkeypatch.setattr(qoder_module, "SystemMessage", FakeSystemMessage)
    monkeypatch.setattr(qoder_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(qoder_module, "ToolPermissionContext", FakePermissionContext)
    monkeypatch.setattr(qoder_module, "_resolve_cli", lambda _: "/usr/bin/qodercli")
    monkeypatch.setattr(qoder_module, "qodercli_auth", lambda: {"type": "qodercli"})

    repo = tmp_path / "repo"
    repo.mkdir()
    factory = FakeClientFactory()
    adapter = QoderAdapter(
        QoderSettings(
            enabled=True,
            qoder_bin="qodercli",
            event_idle_timeout_seconds=event_idle_timeout_seconds,
        ),
        client_factory=factory,
    )

    async def available_probe():
        return adapter._runtime_info(available=True, version="test")

    monkeypatch.setattr(adapter, "probe", available_probe)
    service = BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=PolicyRegistry(
            [
                WorkdirAgentPolicy(
                    slot=1,
                    alias="repo",
                    host_path=repo,
                    mode=AgentMode.WORKSPACE_WRITE,
                    runtimes=frozenset({"qoder"}),
                    read_only=False,
                )
            ]
        ),
        adapters={"qoder": adapter},
        lease_manager=LeaseManager(tmp_path / "locks"),
    )
    return service, factory


async def wait_for_status(service: BridgeService, task_id: str, *statuses: str) -> dict[str, Any]:
    for _ in range(500):
        task = service.get_task(task_id)
        if task["status"] in statuses:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError(f"task did not reach {statuses}: {service.get_task(task_id)}")


@pytest.mark.asyncio
async def test_qoder_uses_native_server_environment_without_model_override_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        first = await service.submit_task(
            runtime="qoder",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="first",
        )
        first_done = await wait_for_status(service, first["task_id"], "succeeded")
        assert first_done["final_response"] == "first"
        first_record = service.store.get_task(first["task_id"])
        assert first_record.native_session_id == "qoder-session-1"
        assert first_record.native_turn_id == "qoder-result-uuid"

        first_options = factory.options[0]
        assert first_options.cwd == service.policies.get("repo").host_path
        assert str(first_options.cli_path) == "/usr/bin/qodercli"
        assert first_options.auth == {"type": "qodercli"}
        assert first_options.setting_sources == ["user", "project", "local"]
        assert first_options.permission_mode is None
        assert first_options.resume is None
        assert first_options.model is None

        second = await service.submit_task(
            runtime="qoder",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="second",
            continue_from_task_id=first["task_id"],
        )
        second_done = await wait_for_status(service, second["task_id"], "succeeded")
        assert second_done["final_response"] == "second"
        assert factory.options[1].resume == "qoder-session-1"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_qoder_discovers_models_and_accepts_request_scoped_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        catalog = await service.list_models(runtime="qoder")
        assert catalog["status"] == "ok"
        assert catalog["scope"] == "current_account"
        assert [item["id"] for item in catalog["models"]] == [
            "qfmodel",
            "qmodel_38max",
        ]
        assert catalog["models"][0]["is_free"] is True
        discovery_options = factory.options[-1]
        assert discovery_options.setting_sources == ["user"]
        assert discovery_options.model is None
        assert factory.clients[-1].connected is False

        submitted = await service.submit_task(
            runtime="qoder",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="selected",
            model="qfmodel",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["requested_model"] == "qfmodel"
        assert factory.options[-1].model == "qfmodel"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_qoder_permission_round_trip_and_provider_session_suggestions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = make_service(tmp_path, monkeypatch, event_idle_timeout_seconds=0.01)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="qoder",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="approval",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
        pending = waiting["pending_request"]
        assert pending["payload"]["category"] == "command"
        assert pending["payload"]["command_display"] == "echo hello"
        assert "approve_session" in pending["payload"]["available_decisions"]

        await asyncio.sleep(0.05)
        assert service.get_task(submitted["task_id"])["status"] == "waiting_for_approval"

        await service.respond_approval(
            task_id=submitted["task_id"],
            request_id=pending["request_id"],
            decision="approve_session",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "approval:allow:session"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_qoder_persistent_permission_suggestion_is_not_offered_for_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="qoder",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="approval-persistent",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
        pending = waiting["pending_request"]
        assert pending["payload"]["available_decisions"] == [
            "approve_once",
            "deny",
            "cancel_task",
        ]
        await service.respond_approval(
            task_id=submitted["task_id"],
            request_id=pending["request_id"],
            decision="approve_once",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "persistent:allow"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_qoder_ask_user_question_round_trip_preserves_runtime_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="qoder",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="question",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_question")
        pending = waiting["pending_request"]
        question = pending["payload"]["questions"][0]
        assert question["prompt"] == "Which option?"
        assert question["multi_select"] is False
        assert question["allow_free_text"] is True
        assert question["options"][0]["preview"] == "Preview A"

        await service.answer_question(
            task_id=submitted["task_id"],
            request_id=pending["request_id"],
            answers=[{"question_id": "q0", "selected_option_ids": ["B"]}],
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "question:B"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_qoder_duplicate_question_text_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="qoder",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="question-duplicate",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "duplicate:deny"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_qoder_cancel_interrupts_and_records_local_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="qoder",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="wait",
        )
        await wait_for_status(service, submitted["task_id"], "running")
        for _ in range(200):
            if factory.clients and factory.clients[0].connected:
                break
            await asyncio.sleep(0.01)

        adapter = service.adapters["qoder"]
        await adapter.cancel(submitted["task_id"])
        await wait_for_status(service, submitted["task_id"], "succeeded")
        assert factory.clients[0].interrupted is True

        reconciliation = await adapter.reconcile_task(service.store.get_task(submitted["task_id"]))
        assert reconciliation.provider_active is False
        assert "disconnected" in reconciliation.detail
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_qoder_runtime_capabilities_are_conservative_before_live_steer_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = make_service(tmp_path, monkeypatch)
    adapter = service.adapters["qoder"]
    info = adapter._runtime_info(available=True, version="1.1.64")
    assert info.persistent_session is True
    assert info.live_steer is False
    assert info.interactive_approval is True
    assert info.interactive_question is True
    assert info.in_flight_recovery == "session-resume"

    with pytest.raises(BridgeError) as exc:
        await adapter.send_message("missing", "hello")
    assert exc.value.code == "AGENT_TASK_NOT_ACTIVE"

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import serverfs_agent_bridge.adapters.claude as claude_module
from serverfs_agent_bridge.adapters.claude import ClaudeAdapter
from serverfs_agent_bridge.config import ClaudeSettings
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode, ReconciliationStatus
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore


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


@dataclass
class FakeResultMessage:
    result: str | None
    session_id: str
    is_error: bool = False
    subtype: str = "success"
    terminal_reason: str = "completed"
    uuid: str | None = None


class FakeClaudeClient:
    def __init__(self, options: Any) -> None:
        self.options = options
        self.prompt: str | None = None
        self.connected = False
        self.interrupted = False

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        del session_id
        self.prompt = prompt

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.connected = False

    async def receive_response(self):
        assert self.prompt is not None
        session_id = self.options.resume or "claude-session-1"

        if self.prompt == "approval":
            suggestion = claude_module.PermissionUpdate(
                type="setMode",
                mode="acceptEdits",
                destination="session",
            )
            context = claude_module.ToolPermissionContext(
                suggestions=[suggestion],
                title="Run command",
                description="Claude wants to run a command",
                tool_use_id="tool-approval",
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

        if self.prompt == "approval-user-settings":
            # A suggestion the Bridge must never turn into a persistent grant.
            suggestion = claude_module.PermissionUpdate(
                type="setMode",
                mode="acceptEdits",
                destination="userSettings",
            )
            context = claude_module.ToolPermissionContext(
                suggestions=[suggestion],
                title="Persist a permission rule",
                tool_use_id="tool-user-settings",
            )
            result = await self.options.can_use_tool(
                "Bash",
                {"command": "echo persistent"},
                context,
            )
            yield FakeResultMessage(
                result=f"user-settings:{result.behavior}",
                session_id=session_id,
            )
            return

        if self.prompt == "question-duplicate":
            context = claude_module.ToolPermissionContext(
                title="Ask duplicate",
                tool_use_id="tool-question-duplicate",
            )
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

        if self.prompt == "question":
            context = claude_module.ToolPermissionContext(
                title="Ask question",
                tool_use_id="tool-question",
            )
            result = await self.options.can_use_tool(
                "AskUserQuestion",
                {
                    "questions": [
                        {
                            "question": "Which option?",
                            "header": "Choice",
                            "multiSelect": False,
                            "options": [
                                {"label": "A", "description": "First"},
                                {"label": "B", "description": "Second"},
                            ],
                        }
                    ]
                },
                context,
            )
            answers = getattr(result, "updated_input", {}).get("answers", {})
            yield FakeResultMessage(
                result=f"question:{answers.get('Which option?')}",
                session_id=session_id,
            )
            return

        if self.prompt == "wait":
            while not self.interrupted:
                await asyncio.sleep(0.01)
            yield FakeResultMessage(
                result="interrupted",
                session_id=session_id,
                terminal_reason="aborted_streaming",
            )
            return

        yield FakeAssistantMessage([FakeTextBlock(self.prompt)])
        yield FakeResultMessage(
            result=self.prompt,
            session_id=session_id,
            uuid="result-uuid",
        )


class FakeClientFactory:
    def __init__(self) -> None:
        self.options: list[Any] = []
        self.clients: list[FakeClaudeClient] = []

    def __call__(self, options: Any) -> FakeClaudeClient:
        self.options.append(options)
        client = FakeClaudeClient(options)
        self.clients.append(client)
        return client


def make_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    event_idle_timeout_seconds: float | None = None,
) -> tuple[BridgeService, FakeClientFactory]:
    monkeypatch.setattr(claude_module, "AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr(claude_module, "TextBlock", FakeTextBlock)
    monkeypatch.setattr(claude_module, "ToolUseBlock", FakeToolUseBlock)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "_resolve_cli", lambda _: "/usr/bin/claude")

    repo = tmp_path / "repo"
    repo.mkdir()
    factory = FakeClientFactory()
    adapter = ClaudeAdapter(
        ClaudeSettings(
            enabled=True,
            claude_bin="claude",
            event_idle_timeout_seconds=event_idle_timeout_seconds,
        ),
        client_factory=factory,
    )
    service = BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=PolicyRegistry(
            [
                WorkdirAgentPolicy(
                    slot=1,
                    alias="repo",
                    host_path=repo,
                    mode=AgentMode.WORKSPACE_WRITE,
                    runtimes=frozenset({"claude"}),
                    read_only=False,
                )
            ]
        ),
        adapters={"claude": adapter},
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
async def test_claude_uses_native_server_environment_and_resumes_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        first = await service.submit_task(
            runtime="claude",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="first",
        )
        first_done = await wait_for_status(service, first["task_id"], "succeeded")
        assert first_done["final_response"] == "first"

        first_options = factory.options[0]
        assert first_options.cwd == service.policies.get("repo").host_path
        assert str(first_options.cli_path) == "/usr/bin/claude"
        assert first_options.setting_sources == ["user", "project", "local"]
        assert first_options.system_prompt == {"type": "preset", "preset": "claude_code"}
        assert first_options.resume is None
        # Native mode must not synthesize any execution-policy override; the
        # user's own Claude configuration stays authoritative.
        assert first_options.permission_mode is None
        assert first_options.allowed_tools == []
        assert first_options.disallowed_tools == []
        assert first_options.mcp_servers == {}
        assert first_options.sandbox is None
        assert first_options.env == {}
        assert first_options.model is None

        second = await service.submit_task(
            runtime="claude",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="second",
            continue_from_task_id=first["task_id"],
        )
        second_done = await wait_for_status(service, second["task_id"], "succeeded")
        assert second_done["final_response"] == "second"
        assert factory.options[1].resume == "claude-session-1"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_claude_permission_round_trip_and_session_suggestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = make_service(tmp_path, monkeypatch, event_idle_timeout_seconds=0.01)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="claude",
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

        # Wait longer than the configured provider idle timeout. The task must
        # remain waiting because human interaction is not provider idleness.
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
async def test_claude_ask_user_question_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="claude",
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

        await service.answer_question(
            task_id=submitted["task_id"],
            request_id=pending["request_id"],
            answers=[
                {
                    "question_id": "q0",
                    "selected_option_ids": ["B"],
                }
            ],
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "question:B"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_claude_cancel_interrupts_active_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="claude",
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

        await service.cancel_task(submitted["task_id"])
        cancelled = await wait_for_status(service, submitted["task_id"], "cancelled")
        assert cancelled["status"] == "cancelled"
        assert factory.clients[0].interrupted is True
        assert service.guard_manager.read(1) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_claude_native_mode_rejects_review_and_live_steer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = make_service(tmp_path, monkeypatch)
    adapter = service.adapters["claude"]
    info = adapter._runtime_info(available=True, version="test")
    assert info.live_steer is False

    review = await service.submit_task(
        runtime="claude",
        workdir="repo",
        path="",
        profile="review",
        prompt="review",
    )
    review_done = await wait_for_status(service, review["task_id"], "failed")
    assert review_done["error_code"] == "AGENT_PROFILE_NOT_ALLOWED"

    with pytest.raises(BridgeError) as exc:
        await adapter.send_message("missing", "hello")
    assert "AGENT_TASK_NOT_ACTIVE" in getattr(exc.value, "code", "")


@pytest.mark.asyncio
async def test_claude_user_settings_suggestion_is_never_offered_for_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="claude",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="approval-user-settings",
        )
        waiting = await wait_for_status(service, submitted["task_id"], "waiting_for_approval")
        pending = waiting["pending_request"]
        # The only Claude suggestion targets userSettings, so a persistent grant
        # must not be selectable through the Bridge.
        assert "approve_session" not in pending["payload"]["available_decisions"]
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
        assert done["final_response"] == "user-settings:allow"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_claude_duplicate_question_text_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="claude",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="question-duplicate",
        )
        # Two questions sharing one answer key would silently overwrite each
        # other, so the adapter denies instead of raising a question.
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["final_response"] == "duplicate:deny"
        assert done["pending_request_id"] is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_claude_live_steer_is_rejected_for_an_active_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="claude",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="wait",
        )
        await wait_for_status(service, submitted["task_id"], "running")

        with pytest.raises(BridgeError) as exc:
            await service.send_message(task_id=submitted["task_id"], message="steer me")
        assert exc.value.code == "AGENT_PROVIDER_ERROR"

        await service.cancel_task(submitted["task_id"])
        await wait_for_status(service, submitted["task_id"], "cancelled")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_claude_restart_reconciliation_does_not_treat_resumable_as_stopped(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "state")
    task = store.create_task(
        task_id="agt_reconcile_claude",
        runtime="claude",
        workdir_alias="repo",
        workdir_slot=1,
        relative_cwd="",
        profile="workspace-write",
        continue_from_task_id=None,
    )
    task = store.set_native_ids(task.task_id, native_session_id="claude-session-1")
    adapter = ClaudeAdapter(ClaudeSettings(enabled=True))

    result = await adapter.reconcile_task(task)

    assert result.status is ReconciliationStatus.SESSION_RESUMABLE
    assert result.provider_active is None
    assert "does not prove" in result.detail

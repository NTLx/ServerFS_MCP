from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import serverfs_agent_bridge.adapters.qoder as qoder_module
from serverfs_agent_bridge.adapters.qoder import QoderAdapter
from serverfs_agent_bridge.bootstrap import RuntimeProxy
from serverfs_agent_bridge.config import QoderSettings
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore

# The module-level `linux_only` this file carried was justified as "tasks are stored in UID/mode
# private state under a flock lease". Phase D replaced that: `LeaseManager.acquire_exclusive` now
# dispatches to `windows_lease` on Windows, so the seam the skip was protecting is already
# cross-platform. The whole suite therefore runs on both, rather than a Windows-only copy of the
# business tests existing alongside it.


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
        # Order matters as much as the calls: the adapter must connect, apply the proxy, and only
        # then start the turn, because a turn that began first would already have provider traffic
        # outside the trust boundary. Recorded rather than asserted here so a test can check it.
        self.calls: list[str] = []
        self.proxy_set: str | None = None

    async def connect(self, prompt: str | None = None) -> None:
        self.calls.append("connect")
        self.prompt = prompt
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.calls.append("query")
        self.prompt = prompt

    async def set_proxy(self, proxy: str | None) -> None:
        self.calls.append("set_proxy")
        self.proxy_set = proxy

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
    use_proxy: bool = False,
    runtime_proxy: Any | None = None,
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
            use_proxy=use_proxy,
            event_idle_timeout_seconds=event_idle_timeout_seconds,
        ),
        client_factory=factory,
        runtime_proxy=runtime_proxy,
    )

    async def available_probe():
        return adapter._runtime_info(available=True, version="test")

    monkeypatch.setattr(adapter, "probe", available_probe)
    # One policy object, used for both the registry and the lease ids, exactly as `main.py` does.
    # On Windows `LeaseManager` pre-creates an artifact per *declared* lease id and skips the legacy
    # slot layout, so a `LeaseManager` built without `lease_ids` leaves the lock directory empty and
    # every workspace-write task then fails with `LOCK_PATH_UNSAFE`. The product never hits this
    # because it always passes `policies.lease_ids()`; a hand-built manager has to as well.
    policies = PolicyRegistry(
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
    )
    service = BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=policies,
        adapters={"qoder": adapter},
        lease_manager=LeaseManager(tmp_path / "locks", lease_ids=policies.lease_ids()),
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


class TestRuntimeProxyIsAppliedOnlyAsALocalControlRequest:
    """The endpoint's route to the child, and the two ways that can go wrong.

    `set_proxy` is a control request over the local JSONL channel, so the endpoint never reaches the
    child through argv, the environment or a file. The order is the other half: the proxy has to be
    applied *before* the turn starts, because a turn that began first would already have provider
    traffic outside the trust boundary.
    """

    @pytest.mark.asyncio
    async def test_the_turn_starts_only_after_the_proxy_is_applied(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service, factory = make_service(
            tmp_path,
            monkeypatch,
            use_proxy=True,
            runtime_proxy=RuntimeProxy(url="http://proxy.invalid:8080", no_proxy=""),
        )
        await service.start()
        try:
            submitted = await service.submit_task(
                runtime="qoder",
                workdir="repo",
                path="",
                prompt="hello",
                profile="workspace-write",
            )
            await wait_for_status(service, submitted["task_id"], "succeeded")
            client = factory.clients[-1]
            assert client.calls == ["connect", "set_proxy", "query"], client.calls
            assert client.proxy_set == "http://proxy.invalid:8080"
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_the_endpoint_never_reaches_the_client_options(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`options.proxy` is None even when a proxy is configured.

        Setting it would emit `--proxy <url>` on the qodercli command line, which the frozen
        trust-boundary rule forbids. The fake records the options so a regression would be visible
        here rather than only on a real host.
        """
        service, factory = make_service(
            tmp_path,
            monkeypatch,
            use_proxy=True,
            runtime_proxy=RuntimeProxy(url="http://proxy.invalid:8080", no_proxy=""),
        )
        await service.start()
        try:
            submitted = await service.submit_task(
                runtime="qoder",
                workdir="repo",
                path="",
                prompt="hello",
                profile="workspace-write",
            )
            await wait_for_status(service, submitted["task_id"], "succeeded")
            options = factory.clients[-1].options
            assert options.proxy is None
            # The endpoint is absent from the environment too; only deletions are sent.
            assert all(value is None for value in options.env.values())
            assert not any("proxy.invalid" in str(v) for v in options.env.values())
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_use_proxy_without_an_endpoint_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Policy says route through the Agent proxy, so a missing endpoint must not go direct.

        Connecting anyway would start a qodercli and then send provider traffic outside the
        boundary, which is the outcome the proxy exists to prevent. The refusal happens before the
        client is asked to do anything, so a qodercli is never started on the way to the failure.

        A task submission is asynchronous by design -- it returns before the turn runs -- so the
        refusal surfaces on the task's terminal state rather than at the call. What matters is the
        shape: the task fails as not-ready, and neither `set_proxy` nor the turn itself ever ran.
        """
        service, factory = make_service(tmp_path, monkeypatch, use_proxy=True, runtime_proxy=None)
        await service.start()
        try:
            submitted = await service.submit_task(
                runtime="qoder",
                workdir="repo",
                path="",
                prompt="hello",
                profile="workspace-write",
            )
            failed = await wait_for_status(service, submitted["task_id"], "failed")
            assert failed["error_code"] == "AGENT_RUNTIME_NOT_READY"
            for client in factory.clients:
                assert "set_proxy" not in client.calls, client.calls
                assert "query" not in client.calls, client.calls
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_use_proxy_false_still_scrubs_but_never_sets_a_proxy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`use_proxy=false` means "outside the trust domain", not "inherit the host's proxy".

        The ambient names are still removed, and `set_proxy` is still never called.
        """
        for name in ("HTTPS_PROXY", "ALL_PROXY", "CODEBUDDY_SERVICE_PROXY_URL"):
            monkeypatch.setenv(name, "http://127.0.0.1:9")
        service, factory = make_service(tmp_path, monkeypatch, use_proxy=False)
        await service.start()
        try:
            submitted = await service.submit_task(
                runtime="qoder",
                workdir="repo",
                path="",
                prompt="hello",
                profile="workspace-write",
            )
            await wait_for_status(service, submitted["task_id"], "succeeded")
            client = factory.clients[-1]
            assert "set_proxy" not in client.calls, client.calls
            options = client.options
            for name in ("HTTPS_PROXY", "ALL_PROXY", "CODEBUDDY_SERVICE_PROXY_URL"):
                assert options.env.get(name, "absent") is None, name
        finally:
            await service.close()

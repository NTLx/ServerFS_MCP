from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import serverfs_agent_bridge.adapters.claude as claude_module
from serverfs_agent_bridge.adapters.claude import ClaudeAdapter
from serverfs_agent_bridge.bootstrap import RuntimeProxy
from serverfs_agent_bridge.config import ClaudeSettings
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.lease_identity import slot_lease_id
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import TERMINAL_STATUSES, AgentMode, ReconciliationStatus
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

        if self.prompt == "init-wait":
            # The real provider announces the native session in the init system message, long
            # before the turn completes. This prompt yields exactly that shape and then holds
            # the turn open, so the test can prove the session is durable *mid-turn*.
            yield claude_module.SystemMessage(
                subtype="init",
                data={"session_id": "claude-session-early"},
            )
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
    use_proxy: bool = False,
    runtime_proxy: RuntimeProxy | None = None,
) -> tuple[BridgeService, FakeClientFactory]:
    monkeypatch.setattr(claude_module, "AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr(claude_module, "TextBlock", FakeTextBlock)
    monkeypatch.setattr(claude_module, "ToolUseBlock", FakeToolUseBlock)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "_resolve_cli", lambda _: "claude-fake")

    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    factory = FakeClientFactory()
    adapter = ClaudeAdapter(
        ClaudeSettings(
            enabled=True,
            claude_bin="claude",
            event_idle_timeout_seconds=event_idle_timeout_seconds,
            use_proxy=use_proxy,
        ),
        client_factory=factory,
        runtime_proxy=runtime_proxy,
    )

    async def available_probe():
        return adapter._runtime_info(available=True, version="test")

    monkeypatch.setattr(adapter, "probe", available_probe)
    # One policy object, used for both the registry and the lease ids, exactly as `main.py` does.
    # On Windows `LeaseManager` pre-creates an artifact per *declared* lease id and skips the
    # legacy slot layout, so a `LeaseManager` built without `lease_ids` leaves the lock directory
    # empty and every workspace-write task then fails with `LOCK_PATH_UNSAFE`. The product never
    # hits this because it always passes `policies.lease_ids()`; a hand-built manager has to as
    # well. (The stale module-level `linux_only` skip this fixture once justified was removed:
    # Phase D dispatched the lease backend, so the flock reason no longer describes any code.)
    policies = PolicyRegistry(
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
    )
    service = BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=policies,
        adapters={"claude": adapter},
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
        assert str(first_options.cli_path) == "claude-fake"
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
        # Outside the Agent proxy trust domain the adapter omits `env` entirely (pinned by
        # test_claude_never_passes_env_none_to_the_sdk): what arrives here is the SDK's own
        # default empty overlay, i.e. pure inheritance of the supervisor-scrubbed environment.
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
async def test_claude_reports_discovery_unsupported_and_accepts_explicit_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        catalog = await service.list_models(runtime="claude")
        assert catalog == {
            "runtime": "claude",
            "status": "unsupported",
            "scope": "none",
            "source": "claude_code",
            "models": [],
        }
        submitted = await service.submit_task(
            runtime="claude",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="selected",
            model="claude-sonnet-4-5",
        )
        done = await wait_for_status(service, submitted["task_id"], "succeeded")
        assert done["requested_model"] == "claude-sonnet-4-5"
        assert factory.options[-1].model == "claude-sonnet-4-5"
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
        for _ in range(200):
            if submitted["task_id"] not in service._background:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("cancelled Claude task background cleanup did not finish")
        assert service.guard_manager.read(slot_lease_id(1)) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_claude_normal_result_after_cancel_proves_local_stop(
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

        adapter = service.adapters["claude"]
        await adapter.cancel(submitted["task_id"])
        await wait_for_status(service, submitted["task_id"], "succeeded")

        reconciliation = await adapter.reconcile_task(service.store.get_task(submitted["task_id"]))
        assert reconciliation.provider_active is False
        assert "disconnected" in reconciliation.detail
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
async def test_claude_persists_the_native_session_before_the_turn_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G2 real-run finding (2026-10-09), maintainer-approved: session identity must be durable
    *before* the turn completes.

    The real provider announces the native session in the ``init`` system message; the adapter
    used to discard that message and persist the session only from the ``ResultMessage``, so a
    crash mid-turn was artificially classified ``NOT_RECOVERABLE``. The Qoder adapter already
    persists from ``init``; this pins the same behaviour for Claude, including the dual write to
    the recovery guard, while the public projection keeps hiding both ids.
    """
    service, _factory = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="claude",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="init-wait",
        )
        task_id = submitted["task_id"]

        # Bounded polling for a non-terminal task whose store row already carries the session:
        # "durable before the result" is the property under test, and a fixed sleep would be a
        # readiness guess rather than an observation.
        deadline = time.monotonic() + 10
        record = None
        while time.monotonic() < deadline:
            record = service.store.get_task(task_id)
            if record.status not in {"queued"} and record.native_session_id:
                break
            await asyncio.sleep(0.05)
        assert record is not None, "task was never created"
        assert record.status not in TERMINAL_STATUSES, (
            f"task reached {record.status} before the session could be observed mid-turn"
        )
        assert record.native_session_id == "claude-session-early"
        assert record.native_turn_id is None

        guard = service.guard_manager.read(record.lease_id)
        assert guard is not None and guard.payload.get("task_id") == task_id
        assert guard.payload.get("native_session_id") == "claude-session-early"
        assert guard.payload.get("native_turn_id") is None

        public = service.get_task(task_id)
        assert "native_session_id" not in public and "native_turn_id" not in public

        await service.cancel_task(task_id)
        final = await wait_for_status(service, task_id, "cancelled")
        assert final["status"] == "cancelled"
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


async def _submit_simple_task(service: BridgeService, prompt: str = "plain prompt") -> None:
    submitted = await service.submit_task(
        runtime="claude",
        workdir="repo",
        path="",
        profile="workspace-write",
        prompt=prompt,
    )
    await wait_for_status(service, submitted["task_id"], "succeeded")


def _standalone_python() -> Path:
    """A real interpreter that runs from anywhere.

    ``sys.executable`` inside the test venv is a launcher that locates ``pyvenv.cfg`` relative to
    itself, so a copy of it refuses to run from elsewhere (measured: exit code 106). The base
    interpreter has no such dependency, which is what makes it usable both directly and as the
    binary the PATH-lookup test copies around. Layout differs by platform: a Windows install
    keeps ``python.exe`` at the prefix root, a POSIX install keeps ``python3`` under ``bin/``.
    """
    if sys.platform == "win32":
        return Path(sys.base_prefix) / "python.exe"
    return Path(sys.base_prefix) / "bin" / "python3"


@pytest.mark.asyncio
async def test_claude_probe_reports_unavailable_without_a_resolvable_binary() -> None:
    adapter = ClaudeAdapter(
        ClaudeSettings(enabled=True, claude_bin="claude-not-installed-anywhere-xyz")
    )
    info = await adapter.probe()
    assert info.available is False
    assert info.version is None

    disabled = ClaudeAdapter(ClaudeSettings(enabled=False))
    assert (await disabled.probe()).available is False


@pytest.mark.asyncio
async def test_claude_probe_runs_a_real_version_child_without_the_runtime_proxy() -> None:
    # A real child, not a stub: the interpreter exits 0 and prints a version string, so the
    # probe's spawn/parse path is exercised exactly as production runs it.
    adapter = ClaudeAdapter(ClaudeSettings(enabled=True, claude_bin=str(_standalone_python())))
    info = await adapter.probe()
    assert info.available is True
    assert info.version is not None and info.version.startswith("Python")

    # The probe is a local `--version` child with no provider traffic, so it is usable even when
    # proxy policy demands an endpoint that was never supplied. Fail-closed for a missing proxy
    # endpoint guards the *turn* path (_run), not the local probe; pinning that keeps someone
    # from tightening the probe into a config validation it was never meant to be.
    proxy_needy = ClaudeAdapter(
        ClaudeSettings(enabled=True, claude_bin=str(_standalone_python()), use_proxy=True),
        runtime_proxy=None,
    )
    assert (await proxy_needy.probe()).available is True


@pytest.mark.asyncio
async def test_claude_probe_resolves_a_bare_binary_name_through_path_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The deployment shape on Windows: a bare `claude_bin` resolved through PATH/PATHEXT rather
    # than an absolute path. A copy of a standalone interpreter named like an executable makes
    # `shutil.which` do the real lookup. copyfile copies contents but not the mode, so the copy
    # needs the execute bit back or the POSIX lookup refuses it.
    binary_name = "claude-fake.exe" if sys.platform == "win32" else "claude-fake"
    copied = tmp_path / binary_name
    shutil.copyfile(_standalone_python(), copied)
    copied.chmod(copied.stat().st_mode | 0o111)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    adapter = ClaudeAdapter(ClaudeSettings(enabled=True, claude_bin="claude-fake"))
    info = await adapter.probe()
    assert info.available is True
    assert info.version is not None and info.version.startswith("Python")


@pytest.mark.asyncio
async def test_claude_never_passes_env_none_to_the_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G2 real-run finding (2026-10-08), pinned so it cannot come back.

    The frozen SDK pin treats an explicit ``env=None`` as a hard error inside its spawn path
    (``'NoneType' object is not a mapping``, measured against a real turn) because the field's
    real default is an empty dict, not ``None``. The proxy-free arm must therefore *omit* the
    parameter entirely -- which is also the honest shape, since absence is exactly what pure
    inheritance means. The proxy arm must pass a real mapping, never ``None``.
    """
    captured: list[dict] = []

    class _RecordingOptions(claude_module.ClaudeAgentOptions):
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(claude_module, "ClaudeAgentOptions", _RecordingOptions)

    free_service, _ = make_service(tmp_path, monkeypatch)
    await free_service.start()
    try:
        await _submit_simple_task(free_service)
    finally:
        await free_service.close()
    assert captured, "the proxy-free turn never constructed options"
    assert "env" not in captured[0], (
        "the proxy-free arm passed env to the SDK; an explicit None crashes the frozen pin's "
        "spawn path, and omission is the correct shape for pure inheritance"
    )

    captured.clear()
    proxy_service, _ = make_service(
        tmp_path / "proxy-arm",
        monkeypatch,
        use_proxy=True,
        runtime_proxy=RuntimeProxy(url="http://203.0.113.7:3128", no_proxy=""),
    )
    await proxy_service.start()
    try:
        await _submit_simple_task(proxy_service)
    finally:
        await proxy_service.close()
    assert captured, "the proxy turn never constructed options"
    assert captured[0].get("env") == {
        "HTTPS_PROXY": "http://203.0.113.7:3128",
        "NO_PROXY": "127.0.0.1,localhost,::1",
    }


@pytest.mark.asyncio
async def test_claude_proxy_true_injects_only_https_proxy_and_merged_no_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "http://203.0.113.7:3128"  # TEST-NET-3: documentation-only, never a real host
    service, factory = make_service(
        tmp_path,
        monkeypatch,
        use_proxy=True,
        runtime_proxy=RuntimeProxy(url=endpoint, no_proxy="internal.example.com"),
    )
    await service.start()
    try:
        await _submit_simple_task(service)

        env = factory.options[0].env
        # The overlay is exactly the downward policy's diff against the inherited Bridge
        # environment: the proxy endpoint pair and nothing else. A whole-environment overlay
        # would silently freeze a snapshot of `os.environ` into every turn's options.
        assert env == {
            "HTTPS_PROXY": endpoint,
            # The supervisor has already merged the mandatory bypass into the RuntimeProxy; the
            # mapping re-asserts it so a hand-built RuntimeProxy cannot drop it either.
            "NO_PROXY": "internal.example.com,127.0.0.1,localhost,::1",
        }
        # The endpoint reaches the child only through the overlay env mapping.
        assert endpoint not in str(factory.options[0].system_prompt)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_claude_proxy_true_without_endpoint_fails_closed_before_any_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory = make_service(tmp_path, monkeypatch, use_proxy=True, runtime_proxy=None)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="claude",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="plain prompt",
        )
        done = await wait_for_status(service, submitted["task_id"], "failed")
        # Fail closed, and fail before anything starts: no client is ever constructed, so no
        # claude child can spawn and send provider traffic direct when the policy demanded a
        # proxy. The refusal message names the failure class, never the (absent) endpoint.
        assert done["error_code"] == "AGENT_RUNTIME_NOT_READY"
        assert done["error_message"] == (
            "Claude is configured to use the Agent proxy but no endpoint is available"
        )
        assert factory.options == []
        assert factory.clients == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_claude_proxy_overlay_never_carries_trust_domain_or_provider_native_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Names planted in the Bridge environment the way a polluted deployment would carry them.
    # The tunnel / control-plane / Agent-namespace material must never appear in the overlay;
    # the provider-native ANTHROPIC_* configuration must not either -- not because it would be
    # stripped, but because it reaches the child by inheritance and the overlay is only the
    # diff the policy adds. An overlay carrying ANTHROPIC_* would mean the adapter had started
    # freezing the Bridge environment into every turn.
    monkeypatch.setenv("SERVERFS_AGENT_PROXY_URL", "http://10.1.2.3:9999")
    monkeypatch.setenv("SERVERFS_PROXY_HOST", "10.1.2.3")
    monkeypatch.setenv("CONTROL_PLANE_HTTP_PROXY", "http://10.1.2.3:8888")
    monkeypatch.setenv("TUNNEL_CLIENT_TOKEN", "planted")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://provider.example.internal")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "planted-native-credential")

    service, factory = make_service(
        tmp_path,
        monkeypatch,
        use_proxy=True,
        runtime_proxy=RuntimeProxy(url="http://203.0.113.7:3128", no_proxy=""),
    )
    await service.start()
    try:
        await _submit_simple_task(service)

        env = factory.options[0].env
        assert env is not None
        assert set(env) == {"HTTPS_PROXY", "NO_PROXY"}
        # Even a hand-built RuntimeProxy with an empty operator no_proxy keeps the mandatory
        # loopback bypass, so the child can never proxy its own local control traffic.
        assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"
        for planted in (
            "SERVERFS_AGENT_PROXY_URL",
            "SERVERFS_PROXY_HOST",
            "CONTROL_PLANE_HTTP_PROXY",
            "TUNNEL_CLIENT_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
        ):
            assert planted not in env
    finally:
        await service.close()

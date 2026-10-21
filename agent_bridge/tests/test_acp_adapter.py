from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp.schema import (
    ContentToolCallContent,
    TextContentBlock,
    ToolCallLocation,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
)

from serverfs_agent_bridge.adapters import _RUNTIME_ADAPTERS
from serverfs_agent_bridge.adapters.acp import AcpAdapter, _Client, _permission_payload
from serverfs_agent_bridge.adapters.base import TaskContext
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.models import AcpProfile
from serverfs_agent_bridge.store import TaskStore

FIXTURE = Path(__file__).parent / "fixtures" / "acp_stdio_agent.py"


def test_native_adapter_lazy_registrations_remain_available() -> None:
    assert {"ClaudeAdapter", "CodexAdapter", "QoderAdapter", "AcpAdapter"} <= set(_RUNTIME_ADAPTERS)


def _context(
    tmp_path: Path,
    events: list[tuple[str, dict]],
    recorded: list[tuple[str | None, str | None]],
    *,
    continuation: str | None = None,
    request_approval=None,
) -> TaskContext:
    async def emit(name, payload):
        events.append((name, payload))

    async def record(session_id, turn_id):
        recorded.append((session_id, turn_id))

    async def no_interaction(_payload):
        raise AssertionError("unexpected interaction")

    async def abandon():
        return None

    return TaskContext(
        task_id="task-1",
        workdir="scratch",
        workdir_root=tmp_path,
        cwd=tmp_path,
        profile="workspace-write",
        prompt="exact task prompt",
        requested_model=None,
        continue_native_session_id=continuation,
        emit_event=emit,
        request_approval=request_approval or no_interaction,
        ask_question=no_interaction,
        abandon_interaction=abandon,
        record_native_ids=record,
    )


@pytest.mark.asyncio
async def test_reconciliation_requires_measured_load_capability(tmp_path: Path) -> None:
    adapter = AcpAdapter(AcpProfile("fixture", sys.executable, (str(FIXTURE), "load")))
    task = SimpleNamespace(task_id="task-1", native_session_id="opaque-session")
    before = await adapter.reconcile_task(task)
    assert before.provider_active is None
    assert before.status.value == "UNKNOWN"

    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    await adapter.continue_task(_context(tmp_path, events, recorded, continuation="opaque-session"))
    after = await adapter.reconcile_task(task)
    assert after.provider_active is False
    assert after.status.value == "SESSION_RESUMABLE"


@pytest.mark.asyncio
async def test_probe_does_not_launch_and_run_normalizes_text_and_stops_child(
    tmp_path: Path,
) -> None:
    profile = AcpProfile("fixture", sys.executable, (str(FIXTURE), "normal"))
    adapter = AcpAdapter(profile)
    before = await adapter.probe()
    assert before.available
    assert not before.persistent_session
    assert before.in_flight_recovery == "none"
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    result = await adapter.run_task(_context(tmp_path, events, recorded))
    assert result.native_session_id == "fixture-session"
    assert result.final_response == "fixture response"
    assert recorded == [("fixture-session", None)]
    assert events == [("agent.message", {"text": "fixture response"})]
    assert "raw ACP" not in repr(events)


@pytest.mark.asyncio
async def test_typed_tool_updates_normalize_to_bounded_safe_start_events(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    client = _Client(_context(tmp_path, events, []))
    oversized = "x" * 1000
    sensitive = "never emit /private/secret sensitive output provider_secret hidden"
    start = ToolCallStart(
        session_update="tool_call",
        tool_call_id=oversized,
        title=oversized,
        kind="read",
        status="in_progress",
        raw_input={"secret": sensitive},
        raw_output=sensitive,
        content=[
            ContentToolCallContent(
                type="content",
                content=TextContentBlock(type="text", text=sensitive),
            )
        ],
        locations=[ToolCallLocation(path="/private/secret", line=1, field_meta={"x": sensitive})],
        field_meta={"provider_secret": sensitive},
    )
    update_payload = ToolCallUpdate(
        tool_call_id=oversized,
        title=oversized,
        kind="think",
        status="in_progress",
        raw_input={"secret": sensitive},
        raw_output=sensitive,
        content=[
            ContentToolCallContent(
                type="content",
                content=TextContentBlock(type="text", text=sensitive),
            )
        ],
        locations=[ToolCallLocation(path="/private/secret", line=1)],
        field_meta={"provider_secret": sensitive},
    )
    update = ToolCallProgress.model_validate(
        {**update_payload.model_dump(), "session_update": "tool_call_update"}
    )
    completed_payload = ToolCallUpdate(
        tool_call_id="completed-tool",
        status="completed",
        raw_output=sensitive,
    )
    completed = ToolCallProgress.model_validate(
        {**completed_payload.model_dump(), "session_update": "tool_call_update"}
    )
    invalid_status = ToolCallProgress.model_construct(
        tool_call_id="invalid-status-tool",
        kind="read",
        status="future_status",
        session_update="tool_call_update",
    )

    await client.session_update("session", start)
    await client.session_update("session", update)
    await client.session_update("session", completed)
    await client.session_update("session", invalid_status)
    await client.session_update(
        "session", SimpleNamespace(session_update="future_update", title="ignored")
    )

    assert events == [
        (
            "tool.started",
            {
                "tool_call_id": "x" * 128,
                "title": "x" * 160,
                "kind": "read",
                "status": "in_progress",
            },
        ),
        (
            "tool.started",
            {
                "tool_call_id": "x" * 128,
                "title": "x" * 160,
                "status": "in_progress",
            },
        ),
    ]
    serialized = json.dumps(events)
    for value in (sensitive, "/private/secret", "provider_secret", "hidden"):
        assert value not in serialized


@pytest.mark.asyncio
async def test_protocol_version_mismatch_fails_before_session_creation(tmp_path: Path) -> None:
    adapter = AcpAdapter(AcpProfile("fixture", sys.executable, (str(FIXTURE), "protocol-mismatch")))
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []

    with pytest.raises(BridgeError) as error:
        await adapter.run_task(_context(tmp_path, events, recorded))

    assert error.value.code == "AGENT_PROVIDER_ERROR"
    assert error.value.message == "ACP Agent negotiated an unsupported protocol version"
    assert recorded == []
    assert events == []
    assert adapter._active == {}


@pytest.mark.asyncio
async def test_acp_client_capabilities_are_minimized(tmp_path: Path) -> None:
    cwd = tmp_path.resolve()
    adapter = AcpAdapter(AcpProfile("fixture", sys.executable, (str(FIXTURE), "capabilities")))
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []

    await adapter.run_task(_context(cwd, events, recorded))

    requests = json.loads((cwd / "acp-capabilities.json").read_text(encoding="utf-8"))
    assert requests["initialize"] == {
        "fs": {"readTextFile": False, "writeTextFile": False, "_meta": None},
        "terminal": False,
        "session": None,
        "plan": None,
        "auth": {"terminal": False, "_meta": None},
        "elicitation": None,
        "nes": None,
        "positionEncodings": None,
        "_meta": None,
    }
    assert requests["session"] == {
        "cwd": str(cwd),
        "mcp_servers": [],
        "additional_directories": None,
    }


@pytest.mark.asyncio
async def test_snapshot_hydrates_after_restart_without_process_start(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    store = TaskStore(state_dir)
    profile = AcpProfile("fixture", sys.executable, (str(FIXTURE), "load"))
    first = AcpAdapter(
        profile,
        load_capabilities=store.load_runtime_capabilities,
        save_capabilities=store.save_runtime_capabilities,
    )
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    await first.run_task(_context(tmp_path, events, recorded))
    assert store.load_runtime_capabilities("acp-fixture", first._profile_fingerprint)

    reopened_store = TaskStore(state_dir)

    async def must_not_spawn(*args, **kwargs):
        raise AssertionError("runtime listing or hydration launched an ACP process")

    monkeypatch.setattr("serverfs_agent_bridge.adapters.acp.spawn_acp_child", must_not_spawn)
    restarted = AcpAdapter(
        profile,
        load_capabilities=reopened_store.load_runtime_capabilities,
        save_capabilities=reopened_store.save_runtime_capabilities,
    )
    info = await restarted.probe()
    assert info.persistent_session is True
    assert info.interactive_approval is True
    assert info.version is None
    task = SimpleNamespace(task_id="task-1", native_session_id="opaque-session")
    reconciliation = await restarted.reconcile_task(task)
    assert reconciliation.status.value == "SESSION_RESUMABLE"
    assert reconciliation.provider_active is None

    changed_profile = AcpProfile("fixture", sys.executable, (str(FIXTURE), "normal"))
    changed = AcpAdapter(
        changed_profile,
        load_capabilities=reopened_store.load_runtime_capabilities,
    )
    changed_info = await changed.probe()
    assert changed_info.persistent_session is False
    assert changed_info.interactive_approval is False


def test_profile_fingerprint_covers_launch_policy_but_not_proxy_material() -> None:
    from serverfs_agent_bridge.adapters.acp import _profile_fingerprint

    profile = AcpProfile("fixture", sys.executable, ("agent",), use_proxy=True)
    assert _profile_fingerprint(profile) == _profile_fingerprint(
        AcpProfile("fixture", sys.executable, ("agent",), use_proxy=True)
    )
    assert _profile_fingerprint(profile) != _profile_fingerprint(
        AcpProfile("fixture", sys.executable, ("changed",), use_proxy=True)
    )
    assert _profile_fingerprint(profile) != _profile_fingerprint(
        AcpProfile("fixture", sys.executable, ("agent",), use_proxy=False)
    )
    assert _profile_fingerprint(profile) != _profile_fingerprint(
        AcpProfile("fixture", sys.executable, ("agent",), use_proxy=True, probe_timeout_seconds=6)
    )
    assert _profile_fingerprint(profile) != _profile_fingerprint(
        AcpProfile(
            "fixture", sys.executable, ("agent",), use_proxy=True, shutdown_timeout_seconds=6
        )
    )
    assert _profile_fingerprint(profile) != _profile_fingerprint(
        AcpProfile("other", sys.executable, ("agent",), use_proxy=True)
    )


@pytest.mark.asyncio
async def test_permission_request_uses_exact_option_and_normalizes_extension_kind(
    tmp_path: Path,
) -> None:
    profile = AcpProfile("fixture", sys.executable, (str(FIXTURE), "permission"))
    adapter = AcpAdapter(profile)
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    payloads = []

    async def approve(payload):
        payloads.append(payload)
        return {"option_id": "allow-exact"}

    await adapter.run_task(_context(tmp_path, events, recorded, request_approval=approve))
    assert payloads[0] == {
        "category": "provider_permission",
        "title": "Deterministic operation",
        "response_options": [
            {"option_id": "allow-exact", "label": "Allow once", "kind": "allow_once"}
        ],
    }
    assert (await adapter.probe()).interactive_approval is True


def test_permission_payload_bounds_and_unknown_kind_normalization() -> None:
    from types import SimpleNamespace

    tool_call = SimpleNamespace(title="Do thing", name="raw-name", raw_input={"secret": "x"})
    options = [SimpleNamespace(option_id="opaque", name="Future", kind="vendor_future")]
    normalized = _permission_payload(tool_call, options)
    assert normalized["response_options"] == [
        {"option_id": "opaque", "label": "Future", "kind": "other"}
    ]
    assert "raw_input" not in repr(normalized)
    with pytest.raises(BridgeError):
        _permission_payload(tool_call, options * 2 + options * 15)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["load"])
async def test_continuation_uses_advertised_capability_and_suppresses_load_history(
    tmp_path: Path, mode: str
) -> None:
    adapter = AcpAdapter(AcpProfile("fixture", sys.executable, (str(FIXTURE), mode)))
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    result = await adapter.continue_task(
        _context(tmp_path, events, recorded, continuation="stored-session")
    )
    assert result.native_session_id == "stored-session"
    assert result.final_response == "fixture response"
    assert [event[1]["text"] for event in events] == ["fixture response"]


@pytest.mark.asyncio
async def test_continuation_without_resume_or_load_fails_closed(tmp_path: Path) -> None:
    adapter = AcpAdapter(AcpProfile("fixture", sys.executable, (str(FIXTURE), "normal")))
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    with pytest.raises(BridgeError, match="advertises no resume or load") as failure:
        await adapter.continue_task(
            _context(tmp_path, events, recorded, continuation="stored-session")
        )
    assert failure.value.code == "AGENT_SESSION_NOT_RESUMABLE"


@pytest.mark.asyncio
async def test_cancel_terminates_active_child_and_cleans_adapter_state(tmp_path: Path) -> None:
    adapter = AcpAdapter(AcpProfile("fixture", sys.executable, (str(FIXTURE), "blocked")))
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    task = asyncio.create_task(adapter.run_task(_context(tmp_path, events, recorded)))
    for _ in range(200):
        if await adapter.get_state("task-1") == "running":
            break
        await asyncio.sleep(0.01)
    await adapter.cancel("task-1")
    with pytest.raises(BridgeError):
        await asyncio.wait_for(task, timeout=5)
    assert await adapter.get_state("task-1") is None
    assert "prompt-blocked" not in repr(events)


@pytest.mark.asyncio
async def test_abrupt_child_exit_is_bounded_and_stderr_is_not_exposed(tmp_path: Path) -> None:
    adapter = AcpAdapter(AcpProfile("fixture", sys.executable, (str(FIXTURE), "exit")))
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    with pytest.raises(BridgeError, match="ACP Agent protocol or process failure") as failure:
        await adapter.run_task(_context(tmp_path, events, recorded))
    assert failure.value.message == "ACP Agent protocol or process failure"
    assert repr(events) == "[]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "continuation"),
    [
        ("auth-initialize", None),
        ("auth-new-session", None),
        ("auth-load-session", "opaque-session"),
        ("auth-prompt", None),
    ],
)
async def test_acp_auth_required_error_is_normalized_without_peer_details(
    tmp_path: Path, mode: str, continuation: str | None
) -> None:
    adapter = AcpAdapter(AcpProfile("fixture", sys.executable, (str(FIXTURE), mode)))
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    context = _context(tmp_path, events, recorded, continuation=continuation)

    with pytest.raises(BridgeError) as failure:
        if continuation is None:
            await adapter.run_task(context)
        else:
            await adapter.continue_task(context)

    assert failure.value.code == "AGENT_AUTH_REQUIRED"
    assert failure.value.message == "ACP Agent authentication is required"
    assert "fixture-secret" not in str(failure.value)
    assert repr(events) == "[]"


@pytest.mark.asyncio
async def test_non_auth_acp_error_with_auth_message_keeps_generic_handling(
    tmp_path: Path,
) -> None:
    adapter = AcpAdapter(
        AcpProfile("fixture", sys.executable, (str(FIXTURE), "non-auth-initialize"))
    )
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []

    with pytest.raises(BridgeError) as failure:
        await adapter.run_task(_context(tmp_path, events, recorded))

    assert failure.value.code == "AGENT_PROVIDER_ERROR"
    assert failure.value.message == "ACP Agent protocol or process failure"
    assert "fixture-secret" not in str(failure.value)
    assert repr(events) == "[]"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
async def test_normal_teardown_kills_descendant_process_group(tmp_path: Path) -> None:
    adapter = AcpAdapter(AcpProfile("fixture", sys.executable, (str(FIXTURE), "descendant")))
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    await adapter.run_task(_context(tmp_path, events, recorded))
    pid = int((tmp_path / "fixture-descendant.pid").read_text())
    assert not _process_is_running(pid)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
async def test_cancel_kills_descendant_process_group(tmp_path: Path) -> None:
    adapter = AcpAdapter(
        AcpProfile("fixture", sys.executable, (str(FIXTURE), "descendant-blocked"))
    )
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    task = asyncio.create_task(adapter.run_task(_context(tmp_path, events, recorded)))
    pid_file = tmp_path / "fixture-descendant.pid"
    pid = None
    for _ in range(300):
        if pid_file.exists() and await adapter.get_state("task-1") == "running":
            try:
                pid = int(pid_file.read_text())
            except ValueError:
                pass
            else:
                break
        await asyncio.sleep(0.01)
    assert pid is not None
    await adapter.cancel("task-1")
    with pytest.raises(BridgeError):
        await asyncio.wait_for(task, timeout=5)
    assert not _process_is_running(pid)


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    if status.exists() and status.read_text().split()[2] == "Z":
        return False
    return True


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
async def test_parent_cancellation_kills_descendant_process_group(tmp_path: Path) -> None:
    adapter = AcpAdapter(
        AcpProfile("fixture", sys.executable, (str(FIXTURE), "descendant-blocked"))
    )
    events: list[tuple[str, dict]] = []
    recorded: list[tuple[str | None, str | None]] = []
    task = asyncio.create_task(adapter.run_task(_context(tmp_path, events, recorded)))
    pid_file = tmp_path / "fixture-descendant.pid"
    pid = None
    for _ in range(300):
        if pid_file.exists() and await adapter.get_state("task-1") == "running":
            try:
                pid = int(pid_file.read_text())
            except ValueError:
                pass
            else:
                break
        await asyncio.sleep(0.01)
    assert pid is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert not _process_is_running(pid)
    assert await adapter.get_state("task-1") is None

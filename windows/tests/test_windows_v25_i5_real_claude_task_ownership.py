"""One-use Windows installed real-Claude task/ownership smoke contracts.

These tests never invoke native Claude, the network, Task Scheduler, file
mutation outside pytest tmp_path, or a provider model.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.agent_client import AgentBridgeClientError, AgentBridgeRemoteError
from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-real-claude-task-ownership-smoke.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_real_claude_smoke_source", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i5_real_agent_smoke_uses_only_two_scoped_cores_and_one_bridge() -> None:
    mod = _module()
    assert mod.BRIDGE == "i5-shared"
    assert mod.CORES == {"i5-core-a": 18873, "i5-core-b": 18874}
    assert mod.SENTINEL == "I5_REAL_AGENT_OK"
    assert mod.TERMINAL == {"succeeded", "failed", "cancelled", "interrupted"}


def test_i5_real_agent_mcp_result_mapping_accepts_one_owned_task() -> None:
    mod = _module()
    response = SimpleNamespace(
        is_error=False,
        structured_content={"result": {"task_id": "agt_fixture", "status": "queued"}},
    )
    assert mod.result_mapping(response) == {"task_id": "agt_fixture", "status": "queued"}
    source = SimpleNamespace(
        is_error=False,
        structured_content=None,
        content=[
            SimpleNamespace(
                type="text",
                text='{"result":{"task_id":"agt_fixture","status":"queued"}}',
            )
        ],
    )
    assert mod.result_mapping(source) == {"task_id": "agt_fixture", "status": "queued"}


def test_i5_real_agent_mcp_result_mapping_rejects_error() -> None:
    mod = _module()
    with pytest.raises(mod.I5RealTaskError):
        mod.result_mapping(SimpleNamespace(is_error=True))


@pytest.mark.parametrize(
    "event",
    [
        {"event_type": "tool.started"},
        {"event_type": "tool.completed"},
        {"event_type": "command.started"},
        {"event_type": "command.completed"},
        {"event_type": "file_change.started"},
        {"event_type": "file_change.completed"},
    ],
)
def test_i5_real_agent_audit_rejects_every_tool_command_or_file_change(event) -> None:
    mod = _module()
    with pytest.raises(mod.I5RealTaskError):
        mod.assert_no_unsafe_event([{"event_type": "task.started"}, event])


def test_i5_real_agent_audit_accepts_silent_text_only_task() -> None:
    mod = _module()
    assert (
        mod.assert_no_unsafe_event(
            [{"event_type": "task.started"}, {"event_type": "agent.message"}]
        )
        == 2
    )


def test_i5_real_agent_smoke_requires_http_submission_not_direct_bridge_submit() -> None:
    source = _SOURCE.read_text("utf-8")
    for required in (
        "streamable_http_client(url, http_client=http)",
        'session.call_tool(\n                    "submit_agent_task"',
        '"I5_REAL_TASK_SUBMITTED_VIA_CORE_A_HTTP=PASS"',
        'for method in ("task.get", "task.events", "task.result.read", "task.cancel")',
        '"CORE_TASK_NOT_FOUND"',
        '"I5_FOREIGN_CORE_SCOPED_TASK_METHODS=DENIED"',
        '"I5_FOREIGN_CORE_PUBLIC_TASK_LOOKUP=DENIED"',
        '"I5_REAL_CLAUDE_TASK_COMPLETED=PASS"',
        '"I5_REAL_PROVIDER_TOOL_EVENTS=ZERO"',
        '"I5_REAL_CORE_BRIDGE_CLAUDE_OWNERSHIP=PASS"',
    ):
        assert required in source
    assert source.count('session.call_tool(\n                    "submit_agent_task"') == 1
    assert 'client_a.call("task.submit"' not in source
    assert 'client_b.call("task.submit"' not in source
    assert source.count('"prompt": (') == 1


@pytest.mark.skipif(
    sys.platform != "win32", reason="requires native Windows Named Pipe path semantics"
)
def test_i5_scoped_foreign_task_denial_is_masked_for_public_mcp_lookup() -> None:
    """Explicit scope returns CORE_TASK_NOT_FOUND; public shape hides it."""
    client = CoreGrantBridgeClient(
        Path(r"\\.\pipe\i5-owner-test"),
        core_id="i5-core-b",
        bridge_id="i5-shared",
        key_id="key-b",
        secret=b"b" * 32,
        policy_revisions={"Fixture": "a" * 64},
        server_pid_validator=lambda pid: pid > 0,
    )

    async def foreign_task(_method, _params):
        raise AgentBridgeRemoteError("CORE_TASK_NOT_FOUND", "not available")

    client._signed_call = foreign_task

    async def verify() -> None:
        for method in ("task.get", "task.events", "task.result.read", "task.cancel"):
            with pytest.raises(AgentBridgeRemoteError) as raised:
                await client.call(method, {"task_id": "agt_foreign", "workdir": "Fixture"})
            assert raised.value.code == "CORE_TASK_NOT_FOUND"
        for method in ("task.get", "task.cancel"):
            with pytest.raises(AgentBridgeClientError) as raised:
                await client.call(method, {"task_id": "agt_foreign"})
            assert type(raised.value) is AgentBridgeClientError
            assert str(raised.value) == (
                "Bridge v2 Core capability does not authorize this request"
            )

    asyncio.run(verify())


def test_i5_real_agent_smoke_has_bounded_cancel_and_isolated_reverse_cleanup() -> None:
    source = _SOURCE.read_text("utf-8")
    for required in (
        "gate = smoke.load_gate()",
        "smoke.protected_snapshot(gate)",
        "smoke.candidates_disabled(gate)",
        "storage.check_private(fixture, directory=True",
        "load_native_core_bridge_binding(manifest, workdirs)",
        "started.append(",
        "for component, instance in reversed(started):",
        'client_a.call("task.cancel", {"task_id": task_id})',
        "asyncio.wait_for(_run_real_task(keys, bindings), timeout=140)",
        "I5_REAL_PROVIDER_FIXTURE_STOP=",
        "I5_REAL_PROVIDER_PRODUCTION_AND_I1_I4=UNCHANGED",
    ):
        assert required in source
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "storage.prepare(",
        "storage.stage(",
        "shutil.rmtree(",
        ".write_text(",
        ".write_bytes(",
        "print(task_id)",
        "print(response)",
        "print(state)",
        "print(events)",
        "print(token)",
        "ANTHROPIC_API_KEY",
        "secret_hex",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "git push",
    ):
        assert forbidden not in source

r"""Windows two-process MCP-surface E2E over a real Named Pipe (v0.11 Phase B, §32, §34).

    root process (this test)                       bridge process (agent_bridge venv)
      create_server + AgentBridgeClient ---pipe---> BridgeProtocolServer -> BridgeService
                                                       -> codex-named FakeAdapter

Nothing on the hop is substituted: the pipe, the JSON-line envelope, the measured server SID, the
per-workdir Agent policy, the audit-shaped tool results and the Bridge's own error codes are the
code a Windows deployment runs. The runtime name ``codex`` maps to the deterministic
``FakeAdapter`` in the harness, exactly as the Linux E2E harness does, because the MCP public
allowlist is codex/claude/qoder.

What this surface cannot show on Windows yet is a *completed* task. The frozen rule in
``agent_tools._authorize_submit`` requires a native runtime name to submit with the
``workspace-write`` profile, and that profile is the one the writer lease guards — whose Windows
twin is Phase C. So the submit case below asserts that boundary itself: the request travels the
whole path and comes back as the Bridge's own coded refusal. The full
submit/poll/events/result/approval/question/message/cancel lifecycle over the same real pipe and a
real Bridge process is proven in ``agent_bridge/tests/test_windows_pipe_e2e.py``, which drives the
review profile the Bridge accepts.
"""

from __future__ import annotations

import asyncio
import secrets
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import call_error, call_success, error_code
from serverfs_mcp.agent_client import AgentBridgeClient, AgentBridgeUnavailable
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import AGENT_MODE_WORKSPACE_WRITE, Workdir, WorkdirRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PYTHON = REPO_ROOT / "agent_bridge" / ".venv" / "Scripts" / "python.exe"
BRIDGE_HARNESS = REPO_ROOT / "tests" / "e2e" / "bridge_harness.py"
WORKDIR_ALIAS = "repo"

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Named Pipe Agent endpoint")


@pytest.fixture()
def bridge(tmp_path: Path):
    if not BRIDGE_PYTHON.exists():
        pytest.skip(f"bridge virtualenv interpreter is missing at {BRIDGE_PYTHON}")
    pipe_name = rf"\\.\pipe\serverfs-agent-bridge-e2e-{secrets.token_hex(8)}"
    state_dir = tmp_path / "state"
    lock_dir = tmp_path / "locks"
    workdir = tmp_path / "repo"
    # Only the Agent host path is pre-created. The state and lock trees are created by the Bridge
    # process itself, so they get the Bridge's own protected descriptor: %TEMP% on this machine
    # inherits grants for two other user SIDs, and §25 requires the Bridge to refuse such a
    # pre-planted directory rather than quietly re-securing it.
    workdir.mkdir(parents=True)
    process = subprocess.Popen(
        [
            str(BRIDGE_PYTHON),
            str(BRIDGE_HARNESS),
            "--pipe-name",
            pipe_name,
            "--lock-dir",
            str(lock_dir),
            "--state-dir",
            str(state_dir),
            "--workdir",
            str(workdir),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    ready = process.stdout.readline() if process.stdout else ""
    if not ready.startswith("BRIDGE_READY"):
        rest = process.stdout.read() if process.stdout else ""
        pytest.fail(f"the Windows Bridge never announced readiness: {ready!r} {rest[:2000]!r}")
    try:
        yield SimpleNamespace(
            pipe_name=pipe_name,
            state_dir=state_dir,
            lock_dir=lock_dir,
            workdir=workdir,
            process=process,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)


def agent_server(endpoint: str, lock_dir: Path, workdir: Path, *, enabled: bool = True):
    settings = Settings(
        agent_bridge_enabled=enabled,
        agent_bridge_socket=endpoint,
        agent_lock_dir=str(lock_dir),
    )
    policy_workdir = Workdir(
        alias=WORKDIR_ALIAS,
        root=workdir,
        description="Windows Agent E2E workdir",
        read_only=not enabled,
        agent_mode=AGENT_MODE_WORKSPACE_WRITE if enabled else "disabled",
        agent_runtimes=frozenset({"codex"}) if enabled else frozenset(),
    )
    client = AgentBridgeClient(Path(endpoint), timeout_seconds=30.0) if enabled else None
    return create_server(settings, WorkdirRegistry([policy_workdir]), client)


def tool_names(server) -> set[str]:
    async def _names() -> set[str]:
        return {tool.name for tool in await server.list_tools()}

    return asyncio.run(_names())


def test_tool_surface_is_the_ten_agent_tools_plus_the_filesystem_set(bridge) -> None:
    enabled = agent_server(bridge.pipe_name, bridge.lock_dir, bridge.workdir)
    disabled = agent_server(bridge.pipe_name, bridge.lock_dir, bridge.workdir, enabled=False)
    disabled_names = tool_names(disabled)
    assert len(disabled_names) == 11, sorted(disabled_names)
    assert "submit_agent_task" not in disabled_names
    assert len(tool_names(enabled) - disabled_names) == 10


def test_runtime_discovery_over_the_pipe(bridge) -> None:
    server = agent_server(bridge.pipe_name, bridge.lock_dir, bridge.workdir)
    runtimes = call_success(server, "list_agent_runtimes", {})
    assert [item["name"] for item in runtimes["runtimes"]] == ["codex"]
    models = call_success(server, "list_agent_models", {"runtime": "codex"})
    assert models["status"] == "unsupported"
    assert models["models"] == []


def test_read_only_tools_carry_the_bridge_error_codes_over_the_pipe(bridge) -> None:
    server = agent_server(bridge.pipe_name, bridge.lock_dir, bridge.workdir)
    assert (
        error_code(call_error(server, "get_agent_task", {"task_id": "agt_absent"}))
        == "AGENT_TASK_NOT_FOUND"
    )
    assert (
        error_code(call_error(server, "read_agent_task_events", {"task_id": "agt_absent"}))
        == "AGENT_TASK_NOT_FOUND"
    )
    assert (
        error_code(call_error(server, "read_agent_task_result", {"task_id": "agt_absent"}))
        == "AGENT_TASK_NOT_FOUND"
    )
    assert (
        error_code(
            call_error(
                server,
                "respond_agent_approval",
                {"task_id": "agt_absent", "request_id": "req_absent", "decision": "approve_once"},
            )
        )
        == "REQUEST_NOT_FOUND"
    )
    assert (
        error_code(
            call_error(
                server,
                "answer_agent_question",
                {
                    "task_id": "agt_absent",
                    "request_id": "req_absent",
                    "answers": [{"question_id": "q1", "selected_option_ids": ["b"]}],
                },
            )
        )
        == "REQUEST_NOT_FOUND"
    )
    assert (
        error_code(call_error(server, "cancel_agent_task", {"task_id": "agt_absent"}))
        == "AGENT_TASK_NOT_FOUND"
    )


def test_submit_reaches_the_bridge_and_fails_closed_at_the_lease_seam(bridge) -> None:
    """§3: the one seam Phase B did not implement refuses a write task instead of running one.

    The request travels the entire path — MCP tool, strict envelope, pipe, measured client SID,
    Agent policy, task store — and the Bridge stops it at the writer lease. The normalized code
    arriving back through the published surface is the evidence that the boundary holds inside
    the process that owns it, not merely in a test.
    """
    server = agent_server(bridge.pipe_name, bridge.lock_dir, bridge.workdir)
    message = call_error(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": WORKDIR_ALIAS,
            "profile": "workspace-write",
            "prompt": "complete: must not run unleased",
        },
    )
    assert error_code(message) == "BRIDGE_PLATFORM_UNSUPPORTED", message
    assert "writer lease" in message, message


def test_the_bridge_created_its_own_private_state_tree(bridge) -> None:
    server = agent_server(bridge.pipe_name, bridge.lock_dir, bridge.workdir)
    call_error(
        server,
        "submit_agent_task",
        {"runtime": "codex", "workdir": WORKDIR_ALIAS, "prompt": "complete: x"},
    )
    assert (bridge.state_dir / "state.sqlite3").exists()
    assert (bridge.lock_dir / "active").is_dir()


def test_client_reports_an_absent_bridge_without_a_hang() -> None:
    missing = rf"\\.\pipe\serverfs-agent-bridge-absent-{secrets.token_hex(6)}"
    client = AgentBridgeClient(Path(missing), timeout_seconds=2.0)
    started = time.monotonic()
    with pytest.raises(AgentBridgeUnavailable) as raised:
        asyncio.run(client.call("runtime.list", {}))
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, f"the bounded retry took {elapsed:.1f}s"
    # The published outcome is the frozen code-free message: neither the Win32 text, the pipe
    # name nor any identity material reaches the caller (§11).
    assert str(raised.value) in {
        "agent bridge is unavailable",
        "agent bridge request timed out",
    }, str(raised.value)
    assert missing not in str(raised.value)


def test_client_refuses_a_filesystem_endpoint_on_windows(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="named pipe"):
        AgentBridgeClient(tmp_path / "bridge.sock")

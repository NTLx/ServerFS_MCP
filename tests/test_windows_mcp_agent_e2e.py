r"""Windows two-process MCP-surface E2E over a real Named Pipe (v0.11 Phase B, §32, §34).

    root process (this test)                       bridge process (agent_bridge venv)
      create_server + AgentBridgeClient ---pipe---> BridgeProtocolServer -> BridgeService
                                                       -> codex-named FakeAdapter

Nothing on the hop is substituted: the pipe, the JSON-line envelope, the measured server SID, the
per-workdir Agent policy, the audit-shaped tool results and the Bridge's own error codes are the
code a Windows deployment runs. The runtime name ``codex`` maps to the deterministic
``FakeAdapter`` in the harness, exactly as the Linux E2E harness does, because the MCP public
allowlist is codex/claude/qoder.

What Phase B could not show was a *completed* task. The frozen rule in
``agent_tools._authorize_submit`` requires a native runtime name to submit with the
``workspace-write`` profile, and that profile is the one the writer lease guards -- its Windows
twin is Phase C. Phase C closes that gap over the same real pipe and Bridge process, with two
separate cases because they are two separate contracts:

* ``test_a_workspace_write_task_succeeds_and_writes_the_authorized_workdir`` is the acceptance
  case. The turn must reach exactly ``succeeded``, and the Bridge process itself must have written
  a fixed file under the cwd it resolved for the authorized workdir -- which is only observable if
  the turn really completed. It then reads the answer back through the public task readers and
  proves normal-completion cleanup.
* ``test_workspace_write_lease_serializes_the_workdir_until_cancelled`` is the live
  mutual-exclusion case. It holds the workdir against an MCP mutation for the whole turn and frees
  it by cancelling, so it is a contention test and deliberately does not claim a completion.

The full submit/poll/events/result/approval/question/message/cancel lifecycle over the same real
pipe and a real Bridge process is proven in ``agent_bridge/tests/test_windows_pipe_e2e.py``, which
drives the review profile the Bridge accepts.
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
from serverfs_mcp import lease_identity
from serverfs_mcp.agent_client import AgentBridgeClient, AgentBridgeUnavailable
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import AGENT_MODE_WORKSPACE_WRITE, Workdir, WorkdirRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PYTHON = REPO_ROOT / "agent_bridge" / ".venv" / "Scripts" / "python.exe"
BRIDGE_HARNESS = REPO_ROOT / "tests" / "e2e" / "bridge_harness.py"
WORKDIR_ALIAS = "repo"

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Named Pipe Agent endpoint")


def _launch(
    pipe_name: str,
    state_dir: Path,
    lock_dir: Path,
    workdir: Path,
    max_final_response_bytes: int | None = None,
) -> subprocess.Popen[str]:
    """Start a real Bridge process on one endpoint and wait for its readiness line."""
    if not BRIDGE_PYTHON.exists():
        pytest.skip(f"bridge virtualenv interpreter is missing at {BRIDGE_PYTHON}")
    command = [
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
    ]
    if max_final_response_bytes is not None:
        command += ["--max-final-response-bytes", str(max_final_response_bytes)]
    process = subprocess.Popen(
        command,
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
    return process


def _terminate(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


@pytest.fixture()
def bridge(tmp_path: Path):
    yield from _bridge_fixture(tmp_path, "e2e")


@pytest.fixture()
def spooled_bridge(tmp_path: Path):
    """A Bridge whose inline response bound is below the harness's fixed success answer.

    ``read_agent_task_result`` reads the result spool, and an inline result is refused with
    ``AGENT_RESULT_NOT_RETRIEVABLE`` by contract. Lowering the bound here rather than enlarging the
    prompt keeps the published readers on their real path with a prompt at its ordinary size.
    """
    yield from _bridge_fixture(tmp_path, "spool", max_final_response_bytes=8)


def _bridge_fixture(
    tmp_path: Path, tag: str, *, max_final_response_bytes: int | None = None
) -> SimpleNamespace:
    pipe_name = rf"\\.\pipe\serverfs-agent-bridge-{tag}-{secrets.token_hex(8)}"
    state_dir = tmp_path / "state"
    lock_dir = tmp_path / "locks"
    workdir = tmp_path / "repo"
    # Only the Agent host path is pre-created. The state and lock trees are created by the Bridge
    # process itself, so they get the Bridge's own protected descriptor: %TEMP% on this machine
    # inherits grants for two other user SIDs, and §25 requires the Bridge to refuse such a
    # pre-planted directory rather than quietly re-securing it.
    workdir.mkdir(parents=True)
    process = _launch(pipe_name, state_dir, lock_dir, workdir, max_final_response_bytes)
    try:
        yield SimpleNamespace(
            pipe_name=pipe_name,
            state_dir=state_dir,
            lock_dir=lock_dir,
            workdir=workdir,
            process=process,
        )
    finally:
        _terminate(process)


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


def test_workspace_write_lease_serializes_the_workdir_until_cancelled(bridge) -> None:
    """Live mutual exclusion: the lease holds the workdir for the whole turn, and cancel frees it.

    The request travels the whole path — MCP tool, strict envelope, pipe, measured client SID,
    Agent policy, task store, writer lease — and the task is the profile the lease guards. While an
    Agent turn is live, an MCP mutation on the same workdir is refused with the frozen busy code;
    cancelling the turn ends the lease and the same mutation succeeds with the recovery guard gone.
    The artifact the Bridge created is the one ServerFS opened, which is the alias-derived name both
    sides derive independently (§5.3).

    This case deliberately ends in cancellation: it is the live-contention contract, and the
    completed workspace-write contract is
    ``test_a_workspace_write_task_succeeds_and_writes_the_authorized_workdir``.
    """
    server = agent_server(bridge.pipe_name, bridge.lock_dir, bridge.workdir)
    artifact = bridge.lock_dir / lease_identity.lock_artifact_name(
        lease_identity.alias_lease_id(WORKDIR_ALIAS)
    )

    holder = call_success(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": WORKDIR_ALIAS,
            "profile": "workspace-write",
            "prompt": "wait: this turn holds the workdir until it is cancelled",
        },
    )
    holder_id = holder["task_id"]
    assert holder["status"] in {"queued", "starting", "running"}
    _wait_for_status(server, holder_id, {"starting", "running"})
    assert artifact.is_file(), "the Bridge owns lease creation, and it did not create this one"

    busy = call_error(
        server,
        "create_text_file",
        {"workdir": WORKDIR_ALIAS, "path": "during-task.txt", "content": "must wait\n"},
    )
    assert error_code(busy) == "WORKDIR_BUSY", busy

    call_success(server, "cancel_agent_task", {"task_id": holder_id})
    _wait_for_status(server, holder_id, {"cancelled", "interrupted", "failed", "succeeded"})

    call_success(
        server,
        "create_text_file",
        {"workdir": WORKDIR_ALIAS, "path": "after-task.txt", "content": "leased, released\n"},
    )
    assert (bridge.workdir / "after-task.txt").read_bytes() == b"leased, released\n"
    guard = (
        bridge.lock_dir
        / "active"
        / lease_identity.guard_artifact_name(lease_identity.alias_lease_id(WORKDIR_ALIAS))
    )
    assert not guard.exists(), "a terminal task must clear its recovery guard"


def _wait_for_status(server, task_id: str, wanted: set[str], timeout: float = 30.0) -> str:
    """Poll the published reader until the task reaches one of the wanted states."""
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = call_success(server, "get_agent_task", {"task_id": task_id})["status"]
        if status in wanted:
            return status
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} stayed in {status!r} for {timeout}s")


def _wait_for_terminal(server, task_id: str, timeout: float = 30.0) -> str:
    """Poll until the task is terminal and return whichever terminal state it reached.

    The caller must still assert the exact state it requires. Returning the four frozen terminal
    states as one group would let a cancellation or a provider failure satisfy a success assertion.
    """
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = call_success(server, "get_agent_task", {"task_id": task_id})["status"]
        if status in {"succeeded", "failed", "cancelled", "interrupted"}:
            return status
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} stayed in {status!r} for {timeout}s")


def test_a_workspace_write_task_succeeds_and_writes_the_authorized_workdir(spooled_bridge) -> None:
    """Phase C acceptance: a published Windows workspace-write task completes and writes the work.

    This is the completed-task exit requirement Phase B could not meet. The turn is submitted
    through the real surface — MCP tool, strict envelope, Named Pipe, measured client SID, Agent
    policy, task store, writer lease — with the native runtime name the frozen
    ``_authorize_submit`` requires, and it has to reach exactly ``succeeded``.

    The load-bearing assertion is the file. A task that merely returned ``"ok"`` would prove the
    transport and nothing about the workspace-write contract, so the test instead requires the
    Bridge process itself to have written a fixed artifact under the ``cwd`` it resolved for the
    authorized workdir. That single fact ties the workdir policy, the resolved cwd, the
    workspace-write profile, the writer lease held for the task's whole lifetime, and a
    provider-side mutation together — and it is only observable because the turn really finished.
    """
    server = agent_server(spooled_bridge.pipe_name, spooled_bridge.lock_dir, spooled_bridge.workdir)
    lease_id = lease_identity.alias_lease_id(WORKDIR_ALIAS)
    guard = spooled_bridge.lock_dir / "active" / lease_identity.guard_artifact_name(lease_id)

    submitted = call_success(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": WORKDIR_ALIAS,
            "profile": "workspace-write",
            "prompt": "phase-c-workspace-write",
        },
    )
    task_id = submitted["task_id"]

    # Exactly succeeded. A cancelled, interrupted or failed turn fails this test even though the
    # mutation file may already exist, because the completed-task contract is the exit requirement.
    assert _wait_for_terminal(server, task_id) == "succeeded"

    created = spooled_bridge.workdir / "phase-c-agent-write.txt"
    assert created.is_file(), "the completed turn did not write into the authorized workdir"
    assert created.read_bytes() == b"written by workspace-write fake adapter\n"

    events = call_success(server, "read_agent_task_events", {"task_id": task_id})
    types = [event["event_type"] for event in events["events"]]
    assert "turn.started" in types, types
    assert "agent.message" in types, types
    assert "task.completed" in types, types

    # The fixed answer is longer than this Bridge's inline bound, so the result is spooled and the
    # published byte-ranged reader returns the real provider text rather than an inline copy.
    final = call_success(server, "get_agent_task", {"task_id": task_id})
    assert final["status"] == "succeeded", final
    assert final["result"]["storage"] == "spool", final["result"]
    assert final["result"]["retrievable"] is True, final["result"]
    result = call_success(server, "read_agent_task_result", {"task_id": task_id})
    assert result["text"] == "phase-c-workspace-write"
    assert result["eof"] is True

    # Normal completion is its own cleanup contract, distinct from the cancellation path: the
    # guard is gone, the lease is released, and a mutation succeeds immediately with no recovery.
    assert not guard.exists(), "a normally completed task must clear its recovery guard"
    call_success(
        server,
        "create_text_file",
        {"workdir": WORKDIR_ALIAS, "path": "after-success.txt", "content": "released\n"},
    )
    assert (spooled_bridge.workdir / "after-success.txt").read_bytes() == b"released\n"


def test_the_bridge_created_its_own_private_state_tree(bridge) -> None:
    server = agent_server(bridge.pipe_name, bridge.lock_dir, bridge.workdir)
    call_success(server, "list_agent_runtimes", {})
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


def test_a_crashed_bridge_leaves_recovery_state_a_restarted_one_clears(tmp_path: Path) -> None:
    """C4 on Windows: the lease is reclaimed by the OS, the guard is not, and the surface waits.

    ``TerminateProcess`` runs no cleanup at all, so this is the crash matrix rather than a shutdown
    path: the live lock must release, the persistent guard must survive, mutations must be refused
    with recovery state, and a Bridge restarted on the same state and lock trees must reconcile the
    task and clear the guard so the workdir becomes writable again.
    """
    pipe_name = rf"\\.\pipe\serverfs-agent-bridge-crash-{secrets.token_hex(8)}"
    state_dir = tmp_path / "state"
    lock_dir = tmp_path / "locks"
    workdir = tmp_path / "repo"
    workdir.mkdir(parents=True)
    process = _launch(pipe_name, state_dir, lock_dir, workdir)
    server = agent_server(pipe_name, lock_dir, workdir)

    submitted = call_success(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": WORKDIR_ALIAS,
            "profile": "workspace-write",
            "prompt": "wait: this turn is interrupted by the crash",
        },
    )
    task_id = submitted["task_id"]
    _wait_for_status(server, task_id, {"starting", "running"})

    process.kill()
    process.wait(timeout=15)

    guard = (
        lock_dir
        / "active"
        / lease_identity.guard_artifact_name(lease_identity.alias_lease_id(WORKDIR_ALIAS))
    )
    assert guard.is_file(), "a crashed workspace-write turn must leave its guard behind"
    message = call_error(
        server,
        "create_text_file",
        {"workdir": WORKDIR_ALIAS, "path": "during-recovery.txt", "content": "no\n"},
    )
    assert error_code(message) == "WORKDIR_RECOVERY_REQUIRED", message
    assert not (workdir / "during-recovery.txt").exists()

    restarted = _launch(pipe_name, state_dir, lock_dir, workdir)
    try:
        settled = call_success(server, "get_agent_task", {"task_id": task_id})
        assert settled["status"] in {"interrupted", "cancelled", "failed"}, settled["status"]
        assert not guard.exists(), "reconciliation must clear the guard it has terminalized"
        call_success(
            server,
            "create_text_file",
            {"workdir": WORKDIR_ALIAS, "path": "after-recovery.txt", "content": "yes\n"},
        )
        assert (workdir / "after-recovery.txt").read_bytes() == b"yes\n"
    finally:
        _terminate(restarted)


def test_a_held_writer_lease_blocks_agent_workspace_write_acquisition(tmp_path: Path) -> None:
    """Bidirectional exclusion: an outside holder makes the Bridge refuse the lease (§C4).

    The holder is a real second process using the same LockFileEx shape ServerFS's own reader uses,
    which is the production contention: MCP mutation holding the lease, Agent submission refused.
    """
    pipe_name = rf"\\.\pipe\serverfs-agent-bridge-busy-{secrets.token_hex(8)}"
    state_dir = tmp_path / "state"
    lock_dir = tmp_path / "locks"
    workdir = tmp_path / "repo"
    workdir.mkdir(parents=True)
    bridge = _launch(pipe_name, state_dir, lock_dir, workdir)
    server = agent_server(pipe_name, lock_dir, workdir)
    holder = _launch_lease_holder(lock_dir)
    try:
        message = call_error(
            server,
            "submit_agent_task",
            {
                "runtime": "codex",
                "workdir": WORKDIR_ALIAS,
                "profile": "workspace-write",
                "prompt": "complete: must not run while the workdir is held",
            },
        )
        assert error_code(message) == "WORKDIR_BUSY", message
    finally:
        _terminate(holder)
        _terminate(bridge)


_HOLDER_SCRIPT = """
import sys
from pathlib import Path

from serverfs_mcp import windows_lease

with windows_lease.hold(Path(sys.argv[1]), sys.argv[2]):
    print("HELD", flush=True)
    sys.stdin.read()
"""


def _launch_lease_holder(lock_dir: Path) -> subprocess.Popen[str]:
    """Hold one workdir's lease from a separate process, the way a live Agent turn holds it."""
    lease_id = lease_identity.alias_lease_id(WORKDIR_ALIAS)
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(lock_dir), lease_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    line = process.stdout.readline() if process.stdout else ""
    assert line.startswith("HELD"), line
    return process

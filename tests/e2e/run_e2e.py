"""Two-process MCP <-> Agent Bridge E2E harness — the Phase D completion gate.

Both hops are real, in separate processes and separate virtualenvs:

    root environment                          bridge environment
      MCP server (serverfs_mcp)                 BridgeProtocolServer
        + AgentBridgeClient   --AF_UNIX-->        -> BridgeService
                                                  -> codex-named FakeAdapter

The driver launches the bridge with the ``agent_bridge`` virtualenv, then drives
the published MCP surface with the root environment. Nothing is mocked: the
socket hop, the strict request/response envelope, SQLite task persistence and
the cross-process ``flock`` are all exercised as they are in a deployment.

The MCP public runtime allowlist is exactly ``codex``/``claude``, so the harness
exposes the deterministic ``FakeAdapter`` under the name ``codex`` on the bridge
side only (see ``bridge_harness.py``). The production adapter keeps
``name == "fake"`` and never appears in the MCP allowlist.

    uv run python tests/e2e/run_e2e.py
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from serverfs_mcp.agent_client import AgentBridgeClient, AgentBridgeRemoteError
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import (
    AGENT_MODE_WORKSPACE_WRITE,
    Workdir,
    WorkdirRegistry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PYTHON = REPO_ROOT / "agent_bridge" / ".venv" / "bin" / "python"
BRIDGE_HARNESS = Path(__file__).resolve().parent / "bridge_harness.py"
WORKDIR_ALIAS = "repo"
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
AGENT_TOOLS = {
    "list_agent_runtimes",
    "submit_agent_task",
    "get_agent_task",
    "read_agent_task_events",
    "respond_agent_approval",
    "answer_agent_question",
    "send_agent_message",
    "cancel_agent_task",
}
_CODE_RE = re.compile(r"\b([A-Z][A-Z_]{2,}):")


class Report:
    """Collects gate results so the process can exit non-zero on any failure."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0

    def section(self, title: str) -> None:
        print(f"\n== {title} ==")

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        self.checks += 1
        mark = "OK  " if condition else "FAIL"
        suffix = f"  ({detail})" if detail else ""
        print(f"[{mark}] {label}{suffix}")
        if not condition:
            self.failures.append(label)


class McpProbe:
    """Drives the MCP tool surface and captures every payload it returned."""

    def __init__(self, server, report: Report):
        self.server = server
        self.report = report
        self.seen: list[str] = []

    async def ok(self, name: str, args: dict) -> dict:
        result = await self.server.call_tool(name, args)
        if result.is_error:
            raise AssertionError(f"{name} returned an error: {result}")
        payload = result.structured_content or {}
        self.seen.append(json.dumps(payload, ensure_ascii=False, default=str))
        return payload

    async def err(self, name: str, args: dict) -> str:
        try:
            await self.server.call_tool(name, args)
        except ToolError as exc:
            message = str(exc)
            self.seen.append(message)
            return message
        raise AssertionError(f"expected {name} to fail, but it succeeded")

    async def poll(self, task_id: str, statuses: set[str], timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = await self.ok("get_agent_task", {"task_id": task_id})
            status = last.get("status")
            if status in statuses:
                return last
            if status in TERMINAL:
                raise AssertionError(f"task reached terminal {status!r}: {last}")
            await asyncio.sleep(0.05)
        raise AssertionError(f"task did not reach {statuses} within {timeout}s: {last}")


def error_code(message: str) -> str:
    """The CODE: token of a tool error, tolerating the MCP framework prefix."""
    match = _CODE_RE.search(message)
    return match.group(1) if match else message.strip()


async def tool_names(server) -> set[str]:
    return {tool.name for tool in await server.list_tools()}


async def wait_for(predicate, *, timeout: float = 20.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


def build_server(socket_path: Path, lock_dir: Path, workdir: Path):
    settings = Settings(
        agent_bridge_enabled=True,
        agent_bridge_socket=str(socket_path),
        agent_lock_dir=str(lock_dir),
    )
    policy_workdir = Workdir(
        slot=1,
        alias=WORKDIR_ALIAS,
        container_path=workdir,
        description="Two-process E2E workdir",
        read_only=False,
        agent_mode=AGENT_MODE_WORKSPACE_WRITE,
        agent_runtimes=frozenset({"codex"}),
    )
    registry = WorkdirRegistry([policy_workdir])
    return create_server(settings, registry, AgentBridgeClient(socket_path)), policy_workdir


async def start_bridge(socket_path: Path, lock_dir: Path, state_dir: Path, workdir: Path):
    if not BRIDGE_PYTHON.exists():
        raise SystemExit(
            f"bridge virtualenv not found at {BRIDGE_PYTHON}; run `uv sync` in agent_bridge/ first"
        )
    proc = await asyncio.create_subprocess_exec(
        str(BRIDGE_PYTHON),
        str(BRIDGE_HARNESS),
        "--socket",
        str(socket_path),
        "--lock-dir",
        str(lock_dir),
        "--state-dir",
        str(state_dir),
        "--workdir",
        str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
    if line.strip() != b"BRIDGE_READY":
        stderr = (await proc.stderr.read()).decode("utf-8", "replace")
        proc.kill()
        raise SystemExit(f"bridge process failed to start: {line!r}\n{stderr}")
    return proc


async def mutation_succeeds(probe: McpProbe) -> bool:
    """True once the released lease lets a mutation through."""
    try:
        await probe.ok(
            "create_text_file",
            {"workdir": WORKDIR_ALIAS, "path": "after-release.txt", "content": "yes\n"},
        )
        return True
    except ToolError as exc:
        return error_code(str(exc)) == "PATH_ALREADY_EXISTS"


async def run(report: Report, base: Path) -> None:
    workdir = base / "repo"
    workdir.mkdir()
    socket_path = base / "bridge.sock"
    lock_dir = base / "locks"
    state_dir = base / "state"
    state_dir.mkdir(mode=0o700)

    proc = await start_bridge(socket_path, lock_dir, state_dir, workdir)
    try:
        server, _ = build_server(socket_path, lock_dir, workdir)
        probe = McpProbe(server, report)
        low_seen: list[str] = []

        # ---- default (v0.2) surface stays intact -------------------------------
        report.section("tool surface")
        default_names = await tool_names(create_server(Settings(), WorkdirRegistry([])))
        enabled_names = await tool_names(server)
        report.check(
            "default surface is the 11-tool v0.2 set",
            len(default_names) == 11 and "submit_agent_task" not in default_names,
            f"{len(default_names)} tools",
        )
        report.check(
            "global + per-workdir enablement yields 19 tools",
            len(enabled_names) == 19,
            f"{len(enabled_names)} tools",
        )

        # ---- low-level UDS client ---------------------------------------------
        report.section("low-level UDS client (root -> bridge)")
        low = AgentBridgeClient(socket_path, timeout_seconds=30)
        listing = await low.call("runtime.list", {})
        names = [item["name"] for item in listing["runtimes"]]
        report.check("runtime.list over AF_UNIX", names == ["codex"], f"runtimes={names}")

        submission = await low.call(
            "task.submit",
            {
                "runtime": "codex",
                "workdir": WORKDIR_ALIAS,
                "path": "",
                "profile": "workspace-write",
                "prompt": "complete: hello from the driver",
            },
        )
        low_task_id = submission["task_id"]
        report.check(
            "task.submit returns a queued handle", submission["status"] == "queued", low_task_id
        )
        final: dict = {}
        for _ in range(400):
            final = await low.call("task.get", {"task_id": low_task_id})
            if final["status"] in TERMINAL:
                break
            await asyncio.sleep(0.05)
        report.check(
            "task persists and reaches succeeded",
            final.get("status") == "succeeded",
            final.get("final_response", "no response"),
        )
        events = await low.call("task.events", {"task_id": low_task_id})
        low_seen.append(json.dumps(events, ensure_ascii=False))
        report.check(
            "normalized events are readable",
            bool(events["events"]) and events["next_after_event_id"] > 0,
            f"{len(events['events'])} events",
        )
        report.check(
            "SQLite state file exists on the bridge side",
            (state_dir / "state.sqlite3").exists(),
        )
        try:
            await low.call("task.get", {"task_id": "agt_missing"})
            missing_code = "NO_ERROR"
        except AgentBridgeRemoteError as exc:
            missing_code = exc.code
        report.check(
            "unknown task keeps its normalized bridge error code",
            missing_code == "AGENT_TASK_NOT_FOUND",
            missing_code,
        )

        # ---- MCP tools over the real bridge -----------------------------------
        report.section("MCP tools -> real bridge over UDS")
        runtimes = await probe.ok("list_agent_runtimes", {})
        advertised = [item["name"] for item in runtimes["runtimes"]]
        report.check(
            "MCP list_agent_runtimes hides test-only runtimes",
            advertised == ["codex"],
            f"runtimes={advertised}",
        )

        holder = await probe.ok(
            "submit_agent_task",
            {
                "runtime": "codex",
                "workdir": WORKDIR_ALIAS,
                "prompt": "wait: this task never finishes on its own",
            },
        )
        holder_id = holder["task_id"]
        report.check(
            "MCP submit returns a queued handle without waiting",
            holder["status"] == "queued",
            holder_id,
        )
        cross = await low.call("task.get", {"task_id": holder_id})
        low_seen.append(json.dumps(cross, ensure_ascii=False))
        report.check(
            "a task submitted through MCP is visible on the bridge socket",
            cross.get("status") in TERMINAL | {"queued", "starting", "running"},
            str(cross.get("status")),
        )
        # A workspace-write task holds the slot lease for its whole turn, so the
        # remaining checks only run once this one is terminated.
        await probe.ok("cancel_agent_task", {"task_id": holder_id})
        await probe.poll(holder_id, TERMINAL)

        mcp_task = await probe.ok(
            "submit_agent_task",
            {
                "runtime": "codex",
                "workdir": WORKDIR_ALIAS,
                "prompt": "complete: hello through MCP",
            },
        )
        mcp_id = mcp_task["task_id"]
        settled = await probe.poll(mcp_id, TERMINAL)
        report.check(
            "MCP get_agent_task polling reaches succeeded",
            settled.get("status") == "succeeded",
            str(settled.get("final_response")),
        )
        task_events = await probe.ok("read_agent_task_events", {"task_id": mcp_id, "limit": 50})
        report.check(
            "MCP read_agent_task_events returns a cursor",
            bool(task_events["events"]) and task_events["next_after_event_id"] > 0,
            f"{len(task_events['events'])} events",
        )

        # ---- human in the loop -------------------------------------------------
        report.section("human-in-the-loop through the MCP surface")
        approval_task = await probe.ok(
            "submit_agent_task",
            {
                "runtime": "codex",
                "workdir": WORKDIR_ALIAS,
                "prompt": "approval: rm -rf /tmp/serverfs-e2e-scratch",
            },
        )
        approval_id = approval_task["task_id"]
        waiting = await probe.poll(approval_id, {"waiting_for_approval"})
        pending = waiting["pending_request"]
        approval_payload = pending["payload"]
        report.check(
            "get_agent_task surfaces the pending approval",
            pending["kind"] == "approval" and bool(approval_payload.get("command_display")),
            str(approval_payload.get("command_display")),
        )
        resolved = await probe.ok(
            "respond_agent_approval",
            {
                "task_id": approval_id,
                "request_id": pending["request_id"],
                "decision": "approve_once",
            },
        )
        report.check(
            "respond_agent_approval resolves the request", resolved.get("resolved") is True
        )
        approved = await probe.poll(approval_id, TERMINAL)
        report.check(
            "approval resumes the task to succeeded",
            approved.get("status") == "succeeded"
            and "approve_once" in approved.get("final_response", ""),
            str(approved.get("final_response")),
        )
        stale = await probe.err(
            "respond_agent_approval",
            {
                "task_id": approval_id,
                "request_id": pending["request_id"],
                "decision": "approve_once",
            },
        )
        report.check(
            "re-resolving keeps the normalized bridge error code",
            error_code(stale) == "REQUEST_ALREADY_RESOLVED",
            stale,
        )

        question_task = await probe.ok(
            "submit_agent_task",
            {
                "runtime": "codex",
                "workdir": WORKDIR_ALIAS,
                "prompt": "question: Which layer should change?",
            },
        )
        question_id = question_task["task_id"]
        asking = await probe.poll(question_id, {"waiting_for_question"})
        question_request = asking["pending_request"]
        questions = question_request["payload"]["questions"]
        report.check(
            "get_agent_task surfaces the pending question",
            questions and questions[0]["question_id"] == "q1",
            str(questions[0].get("prompt")) if questions else "no questions",
        )
        answered = await probe.ok(
            "answer_agent_question",
            {
                "task_id": question_id,
                "request_id": question_request["request_id"],
                "answers": [
                    {"question_id": "q1", "selected_option_ids": ["b"], "text": "prefer B"}
                ],
            },
        )
        report.check("answer_agent_question resolves the request", answered.get("resolved") is True)
        qsettled = await probe.poll(question_id, TERMINAL)
        report.check(
            "answer resumes the task to succeeded",
            qsettled.get("status") == "succeeded",
            str(qsettled.get("final_response"))[:80],
        )

        # ---- steer and cancel --------------------------------------------------
        report.section("steer and cancel")
        steer_task = await probe.ok(
            "submit_agent_task",
            {
                "runtime": "codex",
                "workdir": WORKDIR_ALIAS,
                "prompt": "steer: start working",
            },
        )
        steer_id = steer_task["task_id"]
        await probe.poll(steer_id, {"running"})
        sent = await probe.ok(
            "send_agent_message",
            {"task_id": steer_id, "message": "focus on the API layer"},
        )
        report.check("send_agent_message is accepted", sent.get("accepted") is True)
        steered = await probe.poll(steer_id, TERMINAL)
        report.check(
            "the steering message reaches the running task",
            "focus on the API layer" in steered.get("final_response", ""),
            str(steered.get("final_response")),
        )

        cancel_task = await probe.ok(
            "submit_agent_task",
            {
                "runtime": "codex",
                "workdir": WORKDIR_ALIAS,
                "prompt": "wait: cancel me",
            },
        )
        cancel_id = cancel_task["task_id"]
        cancelled = await probe.ok("cancel_agent_task", {"task_id": cancel_id})
        report.check(
            "cancel_agent_task reports the terminal state",
            cancelled.get("status") == "cancelled",
            str(cancelled.get("status")),
        )
        await probe.poll(cancel_id, TERMINAL)
        again = await probe.ok("cancel_agent_task", {"task_id": cancel_id})
        report.check(
            "repeated cancellation is safe",
            again.get("status") == "cancelled",
            str(again.get("status")),
        )

        # ---- shared cross-process writer lease ---------------------------------
        report.section("shared cross-process writer lease")
        lease_task = await probe.ok(
            "submit_agent_task",
            {
                "runtime": "codex",
                "workdir": WORKDIR_ALIAS,
                "prompt": "wait: hold the lease",
            },
        )
        lease_id = lease_task["task_id"]
        busy = await probe.err(
            "create_text_file",
            {"workdir": WORKDIR_ALIAS, "path": "blocked.txt", "content": "no\n"},
        )
        report.check(
            "ServerFS mutation reports WORKDIR_BUSY while the agent holds the lease",
            error_code(busy) == "WORKDIR_BUSY",
            busy,
        )
        report.check(
            "the refused mutation wrote nothing",
            not (workdir / "blocked.txt").exists(),
        )
        second = await probe.err(
            "submit_agent_task",
            {
                "runtime": "codex",
                "workdir": WORKDIR_ALIAS,
                "prompt": "complete: this must not start",
            },
        )
        report.check(
            "a second workspace-write task is refused rather than queued",
            error_code(second) == "WORKDIR_BUSY",
            second,
        )

        await probe.ok("cancel_agent_task", {"task_id": lease_id})
        await probe.poll(lease_id, TERMINAL)
        released = await wait_for(lambda: mutation_succeeds(probe), timeout=20)
        report.check("releasing the lease restores ServerFS mutations", released)
        report.check(
            "the mutation after release reached the disk",
            (workdir / "after-release.txt").read_text() == "yes\n",
        )

        # ---- v0.2 compatibility on the agent-enabled server --------------------
        report.section("v0.2 read/mutation/resource channels")
        listing = await probe.ok("list_directory", {"workdir": WORKDIR_ALIAS, "path": ""})
        report.check("list_directory still works", isinstance(listing.get("entries"), list))
        created = await probe.ok(
            "create_text_file",
            {"workdir": WORKDIR_ALIAS, "path": "note.txt", "content": "hello\n"},
        )
        report.check("create_text_file still works", created.get("created") is True)
        read = await probe.ok("read_text_file", {"workdir": WORKDIR_ALIAS, "path": "note.txt"})
        report.check(
            "read_text_file returns content and a revision",
            read.get("content") == "hello\n" and str(read.get("revision", "")).startswith("v1:"),
        )
        found = await probe.ok("find_files", {"workdir": WORKDIR_ALIAS, "pattern": "note.txt"})
        report.check("find_files still works", bool(found.get("matches")))
        searched = await probe.ok("search_text", {"workdir": WORKDIR_ALIAS, "query": "hello"})
        report.check("search_text still works", bool(searched.get("matches")))
        stat = await probe.ok("stat_file", {"workdir": WORKDIR_ALIAS, "path": "note.txt"})
        report.check("stat_file still works", stat.get("type") == "file")

        # ---- host-path confidentiality ----------------------------------------
        report.section("host-path confidentiality")
        blob = "\n".join(probe.seen + low_seen)
        report.check(
            "no host path appears in any MCP or bridge response",
            str(base) not in blob,
            "workdir root is " + str(workdir),
        )
        report.check(
            "no provider-native session/turn id reaches a caller",
            "fake-session" not in blob and "fake-turn" not in blob,
        )
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            proc.kill()
            await proc.wait()


def parse_audit(buffer: str) -> list[dict]:
    """Structured audit lines from the MCP server's stderr."""
    records = []
    for line in buffer.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == "tool_call":
            records.append(payload)
    return records


def check_audit(report: Report, buffer: str, base: Path) -> None:
    report.section("audit records")
    records = [r for r in parse_audit(buffer) if r.get("tool") in AGENT_TOOLS]
    seen = {record["tool"] for record in records}
    report.check(
        "every agent tool emitted an audit record",
        seen == AGENT_TOOLS,
        f"{len(seen)}/{len(AGENT_TOOLS)} tools, {len(records)} records",
    )
    report.check(
        "records carry duration and outcome",
        all("duration_ms" in r and "success" in r for r in records),
    )
    report.check(
        "failed calls carry an error_code",
        all("error_code" in r for r in records if not r["success"]),
    )
    blob = json.dumps(records, ensure_ascii=False)
    report.check("audit never records prompt content", "hello through MCP" not in blob)
    report.check("audit never records steering text", "focus on the API layer" not in blob)
    report.check("audit never records question answers", "prefer B" not in blob)
    report.check("audit never records a host path", str(base) not in blob)
    report.check(
        "send_agent_message logs a byte count instead of the message",
        all("message_bytes" in r for r in records if r["tool"] == "send_agent_message"),
    )
    report.check(
        "submit records workdir, runtime and profile",
        all(
            {"workdir", "runtime", "profile"} <= set(r)
            for r in records
            if r["tool"] == "submit_agent_task"
        ),
    )


def main() -> int:
    report = Report()
    base = Path(tempfile.mkdtemp(prefix="serverfs-e2e-"))
    audit = io.StringIO()
    try:
        with contextlib.redirect_stderr(audit):
            asyncio.run(run(report, base))
        check_audit(report, audit.getvalue(), base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(f"\n{report.checks - len(report.failures)}/{report.checks} checks passed")
    for failure in report.failures:
        print(f"  FAILED: {failure}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())

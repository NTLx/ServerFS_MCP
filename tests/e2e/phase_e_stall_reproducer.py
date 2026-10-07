"""Deterministic reproducer: is the public MCP stall a ServerFS defect, or a live-only artefact?

The live run showed `get_agent_task` taking roughly 15 s over the public MCP surface while a task
sat
in `waiting_for_approval`, while the same `task.get` straight onto the Named Pipe returned in 0.02
s.
Two explanations were available and neither was proven:

- a defect in the path above the Bridge (`AgentBridgeClient` -> Named Pipe -> Bridge), or
- something only a live provider does.

This removes the provider. The chain is the real one -- `serverfs tunnel` CLI, real supervisor, real
Bridge, real Named Pipe, real `serverfs serve` stdio, real public MCP tools -- with a deterministic
provider adapter in the Bridge child that produces a genuine approval through the real
`context.request_approval` path. No network, no Codex, no timing luck.

If the stall reproduces here it is a product defect. If it does not, the live stall's cause is
unknown
and must not be written up as a product finding.

**Control A** uses the production `AgentBridgeClient`: one pipe connection per RPC, which is what
`serve` actually does. The existing Windows pipe tests exercise the server-side pipe contract
through
a different client, so this combination is the gap.

**Control B** goes through the real MCP stdio surface, strictly serialised and demultiplexed by
JSON-RPC id -- one outstanding request, the next only after the previous response matches its id.

No timeout is raised to make a slow result pass. The bound is far above any plausible fast answer
and
far below a real stall, so "slow" and "saturated by a timeout" cannot be confused.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e"))

from serverfs_mcp.agent_client import AgentBridgeClient  # noqa: E402

#: Far above a plausible fast answer, far below a real stall. Never raised to accept a slow result:
#: a bound near the 30 s production timeout would be indistinguishable from the defect.
CALL_BOUND_SECONDS = 5.0

#: Enough to expose a per-connection or per-slot problem, which is typically intermittent.
SEQUENTIAL_CALLS = 20

#: The deterministic adapter raises an approval on this prompt prefix.
APPROVAL_PROMPT = "approval: deterministic-reproducer"

NEWLINE = b"\n"

#: Set from the environment so a failing run can be re-run with its evidence preserved.
_KEEP_TREE = bool(os.environ.get("PHASE_E_KEEP_TREE"))

#: Run the MCP control before the pipe control, to test whether one causes the other to stall.
_B_FIRST = bool(os.environ.get("PHASE_E_B_FIRST"))

#: Set once a control shows a slow or unanswered call, so the tree is preserved automatically.
_reproduced_stall = False


def emit(stage: str, **fields: Any) -> None:
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


def _latency(samples: list[float]) -> dict[str, float] | None:
    if not samples:
        return None
    ordered = sorted(samples)
    return {
        "min_s": round(ordered[0], 3),
        "median_s": round(statistics.median(ordered), 3),
        "max_s": round(ordered[-1], 3),
    }


class IdDemuxReader:
    """One persistent stdout reader that demultiplexes replies by JSON-RPC id.

    Two invariants, both violated by earlier harness versions:

    - a reply is delivered only to the request whose id it carries, so a reply arriving after its
      request timed out can never satisfy the *next* request;
    - such a late reply is recorded, not dropped, so "the chain was slow" stays visible instead of
      looking like a lost response.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._pending: dict[Any, dict[str, Any]] = {}
        self._retired: set[Any] = set()
        self._lock = threading.Lock()
        self.late_ids: list[Any] = []
        self.unmatched_ids: list[Any] = []
        threading.Thread(target=self._pump, daemon=True, name="mcp-id-reader").start()

    def _pump(self) -> None:
        for line in self._stream:
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            identifier = message.get("id")
            with self._lock:
                waiter = self._pending.pop(identifier, None)
                if waiter is None:
                    bucket = self.late_ids if identifier in self._retired else self.unmatched_ids
                    bucket.append(identifier)
            if waiter is not None:
                waiter["message"] = message
                waiter["event"].set()

    def expect(self, identifier: Any, timeout: float) -> dict:
        waiter: dict[str, Any] = {"event": threading.Event(), "message": None}
        with self._lock:
            self._pending[identifier] = waiter
        if not waiter["event"].wait(timeout):
            with self._lock:
                self._pending.pop(identifier, None)
                self._retired.add(identifier)
            raise TimeoutError(f"no response for id {identifier} within {timeout}s")
        return waiter["message"]


class McpSession:
    """One MCP stdio session: a single writer, a single reader, and monotonic ids.

    Several readers on one pipe is exactly the fault this phase already made once in the acceptance
    harness -- each thread calls `readline` on the same stream, so a reply goes to whichever thread
    wins and the loser waits out its own timeout, which reads as a chain that stopped answering.
    There is exactly one reader here and every caller goes through it.
    """

    def __init__(self, process: Any) -> None:
        self.process = process
        self.reader = IdDemuxReader(process.stdout)
        self._next_id = 0
        self._initialized = False

    def call(self, method: str, params: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        self._next_id += 1
        identifier = self._next_id
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        )
        self.process.stdin.write(payload.encode("utf-8") + NEWLINE)
        self.process.stdin.flush()
        return self.reader.expect(identifier, timeout=timeout).get("result", {})

    def initialize(self, timeout: float = 60.0) -> None:
        if self._initialized:
            return
        self.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "phase-e-reproducer", "version": "1"},
            },
            timeout=timeout,
        )
        notice = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.process.stdin.write(notice.encode("utf-8") + NEWLINE)
        self.process.stdin.flush()
        self._initialized = True

    def get_task(self, task_id: str, timeout: float = 60.0) -> dict[str, Any]:
        return _tool_content(
            self.call(
                "tools/call",
                {"name": "get_agent_task", "arguments": {"task_id": task_id}},
                timeout=timeout,
            ),
            "get_agent_task",
        )

    @property
    def late_ids(self) -> list[Any]:
        return self.reader.late_ids

    @property
    def unmatched_ids(self) -> list[Any]:
        return self.reader.unmatched_ids


def _tool_content(result: dict[str, Any], tool: str) -> dict[str, Any]:
    """The tool's payload, with a failing tool reported rather than parsed into a JSON error.

    A `tools/call` error carries human text, not JSON, so parsing it blindly produced a decode error
    that hid the actual message -- one confusing failure on top of the real one.
    """
    if result.get("isError"):
        text = " ".join(
            str(part.get("text", ""))
            for part in result.get("content", [])
            if isinstance(part, dict)
        )
        raise RuntimeError(f"{tool} failed: {text[:200]}")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content") or []
    if not content:
        raise RuntimeError(f"{tool} returned no content")
    return json.loads(str(content[0]["text"]))


def _verify_late_isolation() -> dict[str, Any]:
    """Prove a retired id's late reply cannot satisfy a later request.

    Non-vacuous by construction: request A is abandoned, a reply carrying A's id arrives afterwards
    while B is outstanding, and a reply carrying B's id arrives after that. A reader that ignored
    ids would let A's reply complete B, and the first assertion below would fail.
    """
    reader = IdDemuxReader(iter(()))  # no pump traffic; replies are injected by hand below
    delivered: list[tuple[str, Any]] = []

    def deliver(identifier: Any) -> None:
        with reader._lock:  # noqa: SLF001 - deliberately the reader's own synchronisation
            waiter = reader._pending.pop(identifier, None)
            if waiter is None:
                bucket = "late" if identifier in reader._retired else "unmatched"
                delivered.append((bucket, identifier))
        if waiter is not None:
            waiter["message"] = {"id": identifier}
            waiter["event"].set()

    a_id: Any = "req-A"
    b_id: Any = "req-B"

    with reader._lock:  # noqa: SLF001
        reader._pending[a_id] = {"event": threading.Event(), "message": None}
    with reader._lock:  # noqa: SLF001
        reader._pending.pop(a_id, None)
        reader._retired.add(a_id)

    waiter_b: dict[str, Any] = {"event": threading.Event(), "message": None}
    with reader._lock:  # noqa: SLF001
        reader._pending[b_id] = waiter_b
    deliver(a_id)
    satisfied_by_a = waiter_b["message"] is not None

    deliver(b_id)
    satisfied_by_b = waiter_b["message"] == {"id": b_id}

    return {
        "late_reply_satisfied_next_request": satisfied_by_a,
        "own_reply_satisfied_request": satisfied_by_b,
        "late_reply_recorded_as_late": ("late", a_id) in delivered,
        "ok": (not satisfied_by_a) and satisfied_by_b and ("late", a_id) in delivered,
    }


def _pending_id(lifecycle: Any, task_id: str) -> str | None:
    """The real pending id, read from the store, so the controls can address the request."""
    database = lifecycle.data_home / "agent-bridge" / "state" / "state.sqlite3"
    if not database.exists():
        return None
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT pending_request_id FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _task_snapshot(lifecycle: Any, task_id: str) -> dict[str, Any]:
    """The task row read straight from the store, bypassing MCP entirely.

    When the public surface stops answering, the store is the only remaining witness to what the
    Bridge actually did -- and the difference between "the Bridge never got there" and "the Bridge
    got there and the surface cannot report it" is the whole localisation.
    """
    database = lifecycle.data_home / "agent-bridge" / "state" / "state.sqlite3"
    if not database.exists():
        return {"store_readable": False}
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT status, error_code, pending_request_id FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return {"store_readable": True, "found": False}
        request = con.execute(
            "SELECT kind, status FROM pending_requests WHERE request_id = ?",
            (row["pending_request_id"],),
        ).fetchone()
        return {
            "store_readable": True,
            "found": True,
            "status": row["status"],
            "error_code": row["error_code"],
            "pending_present": bool(row["pending_request_id"]),
            "pending_kind": request["kind"] if request else None,
            "pending_status": request["status"] if request else None,
        }
    finally:
        con.close()


def _wait_for_bridge(lifecycle: Any, timeout: float = 90.0) -> bool:
    """Wait for the supervisor to have started the Bridge and rendered its config.

    The Bridge comes up asynchronously under the supervisor, so its absence immediately after launch
    is the normal startup state rather than a failure.
    """
    from d9_lifecycle import wait_until

    return bool(
        wait_until(
            lambda: lifecycle.bridge_json.is_file() and bool(lifecycle.bridge_pids()),
            timeout=timeout,
        )
    )


async def _control_a(socket_path: str, task_id: str, request_id: str) -> dict[str, Any]:
    """Serial `task.get` over the production client, then the approval on the same client.

    The effective timeout is read from the constructed client rather than from the class default,
    because this phase already produced one wrong root cause by matching a number that merely looked
    familiar: production `serve` uses 30 s, not the constructor default.
    """
    client = AgentBridgeClient(Path(socket_path), timeout_seconds=30.0)
    latencies: list[float] = []
    slow = 0
    error: dict[str, Any] | None = None
    answer: dict[str, Any] = {}
    for index in range(SEQUENTIAL_CALLS):
        started = time.monotonic()
        try:
            result = await client.call("task.get", {"task_id": task_id})
        except Exception as exc:  # noqa: BLE001 - the failure class is itself the measurement
            error = {"class": type(exc).__name__, "message": str(exc)[:120]}
            break
        elapsed = time.monotonic() - started
        latencies.append(elapsed)
        if elapsed >= CALL_BOUND_SECONDS:
            slow += 1
        if index == 0:
            nested = result.get("pending_request") or {}
            if result.get("pending_request_id") != nested.get("request_id"):
                error = {"class": "contract", "message": "pending ids disagree"}
                break
            if result.get("status") != "waiting_for_approval":
                error = {
                    "class": "unexpected-status",
                    "message": str(result.get("status"))[:60],
                }
                break

    if error is None:
        started = time.monotonic()
        try:
            response = await client.call(
                "task.approval.respond",
                {"task_id": task_id, "request_id": request_id, "decision": "approve_once"},
            )
            answer = {
                "outcome": "success",
                "elapsed_s": round(time.monotonic() - started, 3),
                "resolved": bool(response.get("resolved")),
            }
        except Exception as exc:  # noqa: BLE001
            answer = {
                "outcome": "error",
                "class": type(exc).__name__,
                "message": str(exc)[:120],
            }

    return {
        "client": "production AgentBridgeClient, one pipe connection per RPC",
        "effective_timeout_s": client.timeout_seconds,
        "calls_attempted": len(latencies),
        "calls_slow": slow,
        "latency": _latency(latencies),
        **({"error": error} if error else {}),
        "approval": answer,
    }


async def _control_b(session: McpSession, task_id: str) -> dict[str, Any]:
    """Serial `get_agent_task` over real MCP stdio, then the approval through the public tool."""
    session.initialize(timeout=30.0)
    latencies: list[float] = []
    slow = 0
    error: dict[str, Any] | None = None
    for _ in range(SEQUENTIAL_CALLS):
        started = time.monotonic()
        try:
            result = session.call(
                "tools/call",
                {"name": "get_agent_task", "arguments": {"task_id": task_id}},
                timeout=CALL_BOUND_SECONDS * 6,
            )
        except TimeoutError as exc:
            error = {"class": "timeout", "message": str(exc)[:100]}
            break
        elapsed = time.monotonic() - started
        latencies.append(elapsed)
        if elapsed >= CALL_BOUND_SECONDS:
            slow += 1
        if result.get("isError"):
            error = {"class": "tool-error", "message": json.dumps(result)[:100]}
            break

    answer: dict[str, Any] = {}
    if error is None:
        current = session.get_task(task_id, timeout=CALL_BOUND_SECONDS * 6)
        nested = current.get("pending_request") or {}
        started = time.monotonic()
        result = session.call(
            "tools/call",
            {
                "name": "respond_agent_approval",
                "arguments": {
                    "task_id": task_id,
                    "request_id": current.get("pending_request_id"),
                    "decision": "approve_once",
                },
            },
            timeout=CALL_BOUND_SECONDS * 6,
        )
        answer = {
            "outcome": "error" if result.get("isError") else "success",
            "elapsed_s": round(time.monotonic() - started, 3),
            "nested_matches_top": nested.get("request_id") == current.get("pending_request_id"),
        }

    return {
        "surface": "real serverfs serve stdio, public MCP tools",
        "calls_attempted": len(latencies),
        "calls_slow": slow,
        "latency": _latency(latencies),
        "late_responses": sorted(str(i) for i in session.late_ids),
        "unmatched_responses": sorted(str(i) for i in session.unmatched_ids),
        **({"error": error} if error else {}),
        "approval": answer,
    }


def _submit_via_mcp(session: McpSession) -> str:
    """Submit the deterministic approval task through the public MCP surface."""
    session.initialize()
    result = session.call(
        "tools/call",
        {
            "name": "submit_agent_task",
            "arguments": {
                "runtime": "codex",
                "workdir": "repo",
                "path": "",
                "profile": "workspace-write",
                "prompt": APPROVAL_PROMPT,
            },
        },
    )
    return str(_tool_content(result, "submit_agent_task")["task_id"])


def _await_waiting(session: McpSession, task_id: str, timeout: float = 120.0) -> dict[str, Any]:
    """Poll the public surface until the task is waiting for approval."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        content = session.get_task(task_id, timeout=max(5.0, deadline - time.monotonic()))
        if content.get("status") == "waiting_for_approval":
            return content
        time.sleep(0.2)
    raise TimeoutError(f"task never reached waiting_for_approval within {timeout}s")


def _final_state(
    session: McpSession, lifecycle: Any, task_id: str, timeout: float = 90.0
) -> dict[str, Any]:
    """After the approval, the task must finish and the artifact must exist.

    Read through the public surface, so "the approval resolved" and "the provider continued" stay
    separate claims rather than one inferred from the other.
    """
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        content = session.get_task(task_id, timeout=max(5.0, deadline - time.monotonic()))
        status = str(content.get("status"))
        if status in ("succeeded", "failed", "cancelled", "interrupted"):
            break
        time.sleep(0.2)
    artifact = lifecycle.workdir / "reproducer-artifact.txt"
    return {
        "status": status,
        "artifact_present": artifact.exists(),
        "artifact_body": (
            artifact.read_text(encoding="utf-8").strip() if artifact.exists() else None
        ),
    }


def _approval_mode_config(workdir: Path, read_only: bool) -> str:
    """Config for the deterministic approval mode.

    `[agent.codex] enabled = true` keeps the runtime name `codex`, which is the name the test-only
    adapter answers to, so the MCP surface and the frozen tools are exercised against a runtime the
    operator really could have configured. What makes the run deterministic is not the config but
    the
    adapter the D9 harness installs in the Bridge child; with `bridge_mode="approval"` it raises a
    genuine approval through the real `context.request_approval` path. Everything above it --
    supervisor, Bridge, Named Pipe, `serve`, the public MCP tools -- is production code.
    """
    escaped = str(workdir).replace("\\", "\\\\")
    return "\n".join(
        [
            "[server]",
            'log_level = "INFO"',
            "",
            "[agent]",
            "enabled = true",
            "",
            "[agent.codex]",
            "enabled = true",
            "use_proxy = false",
            "",
            "[[workdirs]]",
            'alias = "repo"',
            f'path = "{escaped}"',
            f"read_only = {str(read_only).lower()}",
            'agent_mode = "workspace-write"',
            'agent_runtimes = ["codex"]',
            "",
        ]
    )


def _classify(control_a: dict[str, Any], control_b: dict[str, Any]) -> str:
    a_slow = (control_a.get("calls_slow") or 0) > 0 or "error" in control_a
    b_slow = (control_b.get("calls_slow") or 0) > 0 or "error" in control_b
    if a_slow:
        return "AgentBridgeClient_or_named_pipe"
    if b_slow:
        return "mcp_stdio_layer"
    return "no_deterministic_defect_reproduced"


def main() -> int:
    global _reproduced_stall

    isolation = _verify_late_isolation()
    emit("mcp_id_demux", **isolation)
    if not isolation["ok"]:
        emit("verdict", classification="HARNESS_BROKEN")
        return 2

    from d9_lifecycle import Lifecycle

    tmp_root = Path(tempfile.mkdtemp(prefix="phase-e-reproducer-"))
    lifecycle = None
    try:
        lifecycle = Lifecycle(
            tmp_root,
            agent_enabled=True,
            use_proxy=False,
            read_only=False,
            # Selects the deterministic approval behaviour inside the Bridge child. It must be a
            # constructor argument: `child_env` rebuilds the child environment from scratch, so a
            # value set in `os.environ` afterwards is discarded and the adapter silently runs its
            # default write mode instead.
            bridge_mode="approval",
        )
        lifecycle.config_override = _approval_mode_config(lifecycle.workdir, lifecycle.read_only)
        lifecycle.launch()
        emit(
            "chain",
            bridge_pids=lifecycle.bridge_pids(),
            deterministic=True,
            network=False,
            provider="test-only adapter, approval mode",
        )
        if not _wait_for_bridge(lifecycle, timeout=90.0):
            # A missing rendered config is not a mystery to iterate on: the launcher and supervisor
            # say why on stderr, and that text is the finding.
            emit("bridge_absent", stderr_tail=lifecycle.stderr_text()[-1500:])
            return 3
        emit("bridge_ready", bridge_pids=lifecycle.bridge_pids())

        bridge_config = json.loads(lifecycle.bridge_json.read_text(encoding="utf-8"))
        # One session for the whole run: one writer, one reader, monotonic ids. A reader
        # per phase would put several threads on one pipe, which is the fault that made an
        # earlier version of this look like a chain that had stopped answering.
        session = McpSession(lifecycle.process)
        task_id = _submit_via_mcp(session)
        emit("submitted", task_id=task_id)

        # A timeout reaching the waiting state is itself the finding, so it is reported with the
        # task's real status rather than raised: the store says whether the Bridge got there.
        try:
            pending = _await_waiting(session, task_id)
        except TimeoutError as exc:
            _reproduced_stall = True
            snapshot = _task_snapshot(lifecycle, task_id)
            emit("waiting_timeout", detail=str(exc)[:120], **snapshot)
            if not snapshot.get("pending_present"):
                # No pending request means the task never reached the state under test, so a Control
                # A result would say nothing about it.
                return 4
            # The Bridge is in the waiting state while the public surface cannot report it. That
            # is the defect, and it reproduces with no provider and no network, so the controls
            # run against exactly the state a live run stalled in.
            emit("note", stalled_in_waiting_state=True, deterministic=True)
            request_id = _pending_id(lifecycle, task_id) or ""
        else:
            emit(
                "waiting",
                status=pending.get("status"),
                top_pending_id_present=bool(pending.get("pending_request_id")),
                nested_present=isinstance(pending.get("pending_request"), dict),
            )
            request_id = str(pending.get("pending_request_id"))

        # Order is switchable because the two controls are not independent: Control A opens 20 pipe
        # connections in a burst, and the Bridge serves them from a bounded pool. Running B first
        # separates "the public surface cannot answer at all" from "the public surface stops
        # answering after a burst of pipe traffic", which is a different defect with a different
        # fix.
        if _B_FIRST:
            control_b = asyncio.run(_control_b(session, task_id))
            emit("control_b", **control_b)
            if control_b.get("calls_slow") or "error" in control_b:
                _reproduced_stall = True
            control_a = asyncio.run(_control_a(bridge_config["socket_path"], task_id, request_id))
            emit("control_a", **control_a)
            if control_a.get("calls_slow") or "error" in control_a:
                _reproduced_stall = True
        else:
            control_a = asyncio.run(_control_a(bridge_config["socket_path"], task_id, request_id))
            emit("control_a", **control_a)
            if control_a.get("calls_slow") or "error" in control_a:
                _reproduced_stall = True

            control_b = asyncio.run(_control_b(session, task_id))
            emit("control_b", **control_b)
            if control_b.get("calls_slow") or "error" in control_b:
                _reproduced_stall = True

        final = _final_state(session, lifecycle, task_id)
        emit("final", **final)

        emit("verdict", classification=_classify(control_a, control_b))
        return 0
    finally:
        if lifecycle is not None:
            lifecycle.stop(timeout=45)
            lifecycle.kill()
        # Kept on a failure, removed on success: a reproduced stall needs its TaskStore and rendered
        # config to be inspectable afterwards, and that is exactly when the tree matters.
        if _KEEP_TREE or _reproduced_stall:
            emit("tree_kept", name=tmp_root.name)
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

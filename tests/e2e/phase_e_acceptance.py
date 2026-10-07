"""Phase E §3-§9 acceptance driver: real provider, public MCP surface only.

Runs the gates in the order that keeps failures cheap and attributable: a workspace-write first
(§3-§4), then continuation (§5) and the request-scoped model override (§6), then the two strong
interactive gates (§7 question, §8 approval), then cancellation (§9).

Every pass claim in this file comes from the public MCP surface. The adapter, the transport and the
JSON-RPC layer are never called directly -- they may be used to diagnose, but a diagnostic that
answers the question differently from the product surface does not count as acceptance.

Two rules the maintainer set that this driver enforces rather than assumes:

* **Nothing is pre-created.** Every artifact a task is supposed to produce is asserted absent before
  the task runs, so "the file exists" cannot be satisfied by the harness having written it.
* **Interactive gates are not weakened.** If the provider does not emit a real question or a real
  approval, one bounded prompt variant is tried, and then the gate reports the absence honestly
  rather than having Codex persistent config, approval policy or sandbox authority adjusted to
  manufacture one.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

from phase_e_lifecycle import (  # noqa: E402
    Lifecycle,
)

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}

WORKSPACE_WRITE_PROMPT = (
    "Create a file named phase-e-codex.txt in the current workspace.\n"
    "Its complete contents must be exactly:\n\n"
    "serverfs-phase-e-codex\n\n"
    "Do not modify any other file."
)

CONTINUATION_PROMPT = (
    "Create a second file named phase-e-continuation.txt.\n"
    "Its complete contents must be exactly:\n\n"
    "serverfs-phase-e-continuation\n\n"
    "Do not modify any other file."
)

QUESTION_PROMPT = (
    "Before doing any other work, ask me one non-secret question using your "
    "user-input/question mechanism and wait for my answer.\n\n"
    "Ask: Which marker should I write?\n"
    "Offer two choices: alpha, beta\n\n"
    "After I answer, create question-result.txt containing exactly the selected marker."
)

QUESTION_VARIANT = (
    "Use your user input / ask-user-question tool to ask me which of two markers "
    "(alpha or beta) I want written. Wait for my reply before doing anything else. "
    "Then write my selection into question-result.txt."
)

APPROVAL_PROMPT = (
    "Perform a harmless action that requires your normal approval mechanism before "
    "execution. Prefer a command that only reads or writes inside this workspace. "
    "Do not bypass or suppress an approval request. After the approved action "
    "completes, create approval-result.txt containing exactly:\n\napproved"
)

APPROVAL_VARIANT = (
    "Run a workspace-local shell command (for example listing files with ls) using "
    "your normal tool, and do not suppress or pre-approve it. If it needs approval, "
    "let the approval request happen. Then write approval-result.txt containing "
    "exactly:\n\napproved"
)

CANCEL_PROMPT = (
    "Run a Python command that waits for about 120 seconds before writing "
    "cancel-should-not-complete.txt. Do not finish early."
)

AFTER_CANCEL_PROMPT = "Reply exactly:\n\nafter-cancel"


class McpStdioClient:
    """JSON-RPC over the chain's real stdio. The same shape D9 used, so the acceptance is
    exercising the identical public path rather than a private one."""

    def __init__(self, lifecycle: Lifecycle) -> None:
        self.lifecycle = lifecycle
        self._next_id = 0
        self._inbox: queue.Queue[dict | BaseException] | None = None
        #: Every approval request answered per task, recorded so the evidence distinguishes an
        #: approval the provider actually asked for from one the harness manufactured.
        self.observed_approvals: dict[str, list[str]] = {}
        #: Server-initiated notifications seen while waiting for a response, by method name.
        self.server_notifications: list[str] = []
        #: Ids of replies to requests that already timed out. Non-empty means the chain answered
        #: something after the harness had given up, which is how a slow response and a stuck layer
        #: become distinguishable instead of both reading as "a timeout".
        self.abandoned_responses: list[Any] = []

    def _send(self, payload: dict) -> None:
        assert self.lifecycle.process is not None
        assert self.lifecycle.process.stdin is not None
        self.lifecycle.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.lifecycle.process.stdin.flush()

    def _read(self) -> dict:
        assert self.lifecycle.process is not None
        assert self.lifecycle.process.stdout is not None
        line = self.lifecycle.process.stdout.readline()
        assert line, f"the chain closed stdout: {self.lifecycle.stderr_text()[-2000:]}"
        return json.loads(line.decode("utf-8"))

    def _ensure_reader(self) -> queue.Queue[dict | BaseException]:
        """Start the single stdout reader, once, and return the queue it feeds.

        Exactly one reader for the life of the client. Several would each call ``readline`` on the
        same pipe, so a response could be delivered to whichever thread happened to win, and the
        thread that lost it would block until its own timeout -- turning a healthy chain into an
        apparent hang. The thread outlives individual requests on purpose: after a request gives up,
        the reader keeps draining stdout so the next one starts from a clean stream.
        """
        if self._inbox is None:
            inbox: queue.Queue[dict | BaseException] = queue.Queue()
            self._inbox = inbox

            def pump() -> None:
                try:
                    while True:
                        inbox.put(self._read())
                except BaseException as exc:  # noqa: BLE001 - relayed to callers verbatim
                    inbox.put(exc)
                    return

            threading.Thread(target=pump, daemon=True, name="mcp-reader").start()
        return self._inbox

    def request(self, method: str, params: dict | None = None) -> dict:
        """One JSON-RPC call, with a read that genuinely times out.

        The bound has to wrap a blocking read, not sit between messages. An earlier version checked
        the deadline only at the top of a loop whose body was a blocking ``readline()``, so a chain
        that stopped answering parked the harness forever and the run was eventually reported as a
        timeout -- twice making a stuck MCP layer look like provider latency.
        """
        inbox = self._ensure_reader()
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + 120
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no response to {method} (request {request_id}) within 120s")
            try:
                message = inbox.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(
                    f"no response to {method} (request {request_id}) within 120s"
                ) from exc
            if isinstance(message, BaseException):
                raise message
            if message.get("id") == request_id:
                return message
            # Anything else on this stream is a server-initiated notification or a reply to an
            # abandoned request. Recorded so an unexpected one is visible rather than invisible.
            if "method" in message and "id" not in message:
                self.server_notifications.append(str(message.get("method")))
            else:
                self.abandoned_responses.append(message.get("id"))

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> dict:
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "phase-e-acceptance", "version": "1"},
            },
        )
        self.notify("notifications/initialized")
        return response

    def call(self, name: str, arguments: dict) -> dict:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        result = response["result"]
        if result.get("isError"):
            raise ToolError(name, json.dumps(result)[:600])
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def try_call(self, name: str, arguments: dict) -> dict | None:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        result = response["result"]
        if result.get("isError"):
            return None
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def tool_names(self) -> list[str]:
        return sorted(tool["name"] for tool in self.request("tools/list")["result"]["tools"])

    # -- task helpers ---------------------------------------------------------------

    def submit(
        self,
        prompt: str,
        *,
        model: str | None = None,
        continue_from: str | None = None,
    ) -> str:
        arguments: dict[str, Any] = {
            "runtime": "codex",
            "workdir": "acceptance",
            "path": "",
            "profile": "workspace-write",
            "prompt": prompt,
        }
        if model is not None:
            arguments["model"] = model
        if continue_from is not None:
            arguments["continue_from_task_id"] = continue_from
        return self.call("submit_agent_task", arguments)["task_id"]

    def task(self, task_id: str) -> dict:
        return self.call("get_agent_task", {"task_id": task_id})

    def pending_request(self, task_id: str) -> tuple[str, dict[str, Any]]:
        """The pending interaction, asserting the frozen contract rather than tolerating variants.

        There is exactly one legal shape: a non-empty top-level ``pending_request_id`` *and* a
        nested ``pending_request`` object whose ``request_id`` equals it and whose ``task_id``
        points back at this task. Anything else is a contract failure and raises.

        An earlier version accepted "nested if present, otherwise top-level". That fallback is
        what made a missing id indistinguishable from a present one, so a run that answered
        nothing looked like a run that was answering. Disagreement now fails loudly instead.
        """
        task = self.task(task_id)
        top = task.get("pending_request_id")
        nested = task.get("pending_request")
        problems: list[str] = []
        if not isinstance(top, str) or not top:
            problems.append("top-level pending_request_id missing or empty")
        if not isinstance(nested, dict):
            problems.append("nested pending_request missing or not an object")
        else:
            if nested.get("request_id") != top:
                problems.append("nested request_id does not match pending_request_id")
            if nested.get("task_id") != task_id:
                problems.append("nested task_id does not match the requested task")
        if problems:
            raise ToolError(
                "get_agent_task",
                "pending-request contract failed: " + "; ".join(problems),
            )
        return top, nested

    def offered_decisions(self, nested: dict[str, Any]) -> list[str]:
        """The decisions the provider itself offered, from the one payload key carrying them.

        Read rather than assumed: an approval answered with a decision the provider never
        offered would prove nothing about the round trip.
        """
        payload = nested.get("payload")
        if not isinstance(payload, dict):
            return []
        raw = payload.get("available_decisions")
        return [str(d) for d in raw] if isinstance(raw, list) else []

    def wait_status(self, task_id: str, timeout: float, *, allow_approval: bool = True) -> str:
        """Wait for a terminal status, answering a real approval request if one arrives.

        A real Codex asked to write a file requests approval first -- measured on this host, with
        the provider asking "May I write the requested file in the current workspace?". A wait that
        only watched for terminal states would sit there until the interaction expired and then
        report a timeout rather than what actually happened.

        Answering here is not a workaround: it is the same public tool the approval gate exercises
        deliberately, and every answered request id is recorded, so the evidence stays a count of
        provider-originated requests rather than ones the harness manufactured.

        A failed answer raises. It used to be recorded and swallowed, which made "could not answer"
        indistinguishable from "never saw the request" -- and the run then reported a timeout, which
        is how a harness fault got reported as provider latency.

        ``allow_approval=False`` for the approval gate itself, where the request is the thing under
        test and must be observed rather than silently answered.
        """
        deadline = time.monotonic() + timeout
        status = ""
        approvals: list[str] = []
        while time.monotonic() < deadline:
            task = self.task(task_id)
            status = task["status"]
            if status in TERMINAL:
                self.observed_approvals[task_id] = approvals
                return status
            if status == "waiting_for_approval":
                request_id, nested = self.pending_request(task_id)
                decisions = self.offered_decisions(nested)
                if "approve_once" not in decisions:
                    raise ToolError(
                        "respond_agent_approval",
                        f"provider did not offer approve_once; offered {sorted(decisions)}",
                    )
                if request_id not in approvals:
                    approvals.append(request_id)
                    if allow_approval:
                        self.call(
                            "respond_agent_approval",
                            {
                                "task_id": task_id,
                                "request_id": request_id,
                                "decision": "approve_once",
                            },
                        )
                        self.approval_failures.pop(task_id, None)
            time.sleep(0.2)
        self.observed_approvals[task_id] = approvals
        raise TimeoutError(f"task {task_id} stayed {status!r} for {timeout}s")

    def wait_for(self, task_id: str, statuses: set[str], timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        task: dict = {}
        while time.monotonic() < deadline:
            task = self.task(task_id)
            if task["status"] in statuses:
                return task
            time.sleep(0.1)
        raise TimeoutError(f"task {task_id} stayed {task.get('status')!r} for {timeout}s")

    def events(self, task_id: str) -> list[dict]:
        result = self.call("read_agent_task_events", {"task_id": task_id})
        return result.get("events", [])

    def event_types(self, task_id: str) -> list[str]:
        return sorted(
            {str(e.get("event_type") or e.get("method") or "") for e in self.events(task_id)}
        )

    def read_file(self, path: str) -> str | None:
        result = self.try_call("read_text_file", {"workdir": "acceptance", "path": path})
        if result is None:
            return None
        return str(result.get("content", result.get("text", "")))

    def file_exists_on_disk(self, name: str) -> bool:
        return (self.lifecycle.workdir / name).exists()


class ToolError(AssertionError):
    def __init__(self, tool: str, detail: str) -> None:
        super().__init__(f"{tool} failed: {detail}")
        self.tool = tool


class Acceptance:
    """Collects findings; every assertion records rather than raising, so one failure does not hide
    the rest of the run."""

    def __init__(self, client: McpStdioClient, lifecycle: Lifecycle) -> None:
        self.client = client
        self.lifecycle = lifecycle
        self.findings: dict[str, Any] = {}

    def record(self, key: str, value: Any) -> Any:
        self.findings[key] = value
        return value

    def report(self) -> str:
        return json.dumps(self.findings, indent=2, ensure_ascii=False)


def native_ids(client: McpStdioClient, lifecycle: Lifecycle, task_id: str) -> dict[str, Any]:
    """Read the native thread and turn ids from the TaskStore.

    The public task contract deliberately does not expose them, so this reads the store directly.
    That is a read, not an intervention: no field is added to the MCP surface for acceptance, and
    the ids themselves are never printed or written anywhere -- only their presence.
    """
    import sqlite3

    db = lifecycle.data_home / "agent-bridge" / "state" / "state.sqlite3"
    if not db.exists():
        # Fall back to a scan, because the exact state layout is the Bridge's business and this
        # harness must not depend on it.
        found = list((lifecycle.data_home / "agent-bridge").rglob("*.sqlite3"))
        if not found:
            return {"thread_id_present": False, "turn_id_present": False, "store_found": False}
        db = found[0]
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT native_session_id, native_turn_id FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return {"thread_id_present": False, "turn_id_present": False, "store_found": True}
    return {
        "thread_id_present": bool(row["native_session_id"]),
        "turn_id_present": bool(row["native_turn_id"]),
        "store_found": True,
    }


def _session_of(client: McpStdioClient, task_id: str) -> str | None:
    """The native session id, for comparing continuation against its predecessor."""
    import sqlite3

    db = client.lifecycle.data_home / "agent-bridge" / "state" / "state.sqlite3"
    if not db.exists():
        found = list((client.lifecycle.data_home / "agent-bridge").rglob("*.sqlite3"))
        if not found:
            return None
        db = found[0]
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT native_session_id FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        connection.close()
    return row[0] if row else None


def _turn_of(client: McpStdioClient, task_id: str) -> str | None:
    import sqlite3

    db = client.lifecycle.data_home / "agent-bridge" / "state" / "state.sqlite3"
    if not db.exists():
        found = list((client.lifecycle.data_home / "agent-bridge").rglob("*.sqlite3"))
        if not found:
            return None
        db = found[0]
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT native_turn_id FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        connection.close()
    return row[0] if row else None

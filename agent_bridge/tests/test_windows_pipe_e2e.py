r"""Windows two-process E2E: a real Bridge process, a real pipe, the whole FakeAdapter lifecycle.

    this test process                              bridge subprocess (same venv, own process)
      PipeClient ---Named Pipe---> BridgeProtocolServer -> BridgeService -> codex-named FakeAdapter
                                                            -> SQLite tasks, events, result spool

This is the §32 evidence for the platform: nothing is called in-process, so the pipe hop, the
framing, the measured-and-asserted client SID, the review-profile policy, the task store, the
event log and the result spool are all exercised the way a Windows deployment reaches them. The
runtime name ``codex`` maps to the deterministic ``FakeAdapter`` in the harness, exactly as the
Linux E2E harness does, because the MCP public allowlist is codex/claude/qoder.

The write profile is not driven here: it is the writer lease's, whose Windows twin is Phase C.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pipe_support import PipeClient
from platform_contract import require_windows_kernel
from serverfs_agent_bridge.protocol import MAX_RESPONSE_BYTES

require_windows_kernel("the two-process E2E drives the Windows Bridge over a Named Pipe")

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_HARNESS = REPO_ROOT / "tests" / "e2e" / "bridge_harness.py"
WORKDIR_ALIAS = "repo"
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
# The harness lowers the inline response bound so a prompt-sized answer still reaches the spool.
SPOOL_BOUND_BYTES = 4096


class BridgeProcess:
    def __init__(self, tmp_path: Path):
        self.pipe_name = rf"\\.\pipe\serverfs-agent-bridge-test-{secrets.token_hex(8)}"
        self.state_dir = tmp_path / "state"
        self.lock_dir = tmp_path / "locks"
        self.workdir = tmp_path / "repo"
        self.log = tmp_path / "bridge.log"
        # Only the Agent host path is pre-created: a state or lock tree inherited from a test
        # directory would carry grants for other users, and §25 makes the Bridge refuse that
        # instead of re-securing it. The Bridge creates its own trees, protected.
        self.workdir.mkdir(parents=True)
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        log_handle = self.log.open("w", encoding="utf-8")
        self._process = subprocess.Popen(
            [
                sys.executable,
                str(BRIDGE_HARNESS),
                "--pipe-name",
                self.pipe_name,
                "--lock-dir",
                str(self.lock_dir),
                "--state-dir",
                str(self.state_dir),
                "--workdir",
                str(self.workdir),
                "--runtime-name",
                "fake",
                "--read-only",
                "--max-final-response-bytes",
                str(SPOOL_BOUND_BYTES),
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if "BRIDGE_READY" in self.log.read_text(encoding="utf-8", errors="replace"):
                return
            if self._process.poll() is not None:
                break
            time.sleep(0.05)
        output = self.log.read_text(encoding="utf-8", errors="replace")
        raise AssertionError(f"the Bridge subprocess never became ready:\n{output[-4000:]}")

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=15)

    def client(self) -> PipeClient:
        return PipeClient(self.pipe_name, deadline_seconds=30.0)


@pytest.fixture()
def bridge(tmp_path: Path):
    process = BridgeProcess(tmp_path)
    process.start()
    yield process
    process.stop()
    tail = process.log.read_text(encoding="utf-8", errors="replace")
    assert "Traceback" not in tail, tail[-3000:]


class Session:
    """One persistent connection, so multi-request RPC behaves as a client would see it."""

    def __init__(self, client: PipeClient):
        self.client = client
        self.counter = 0

    def __enter__(self) -> Session:
        self.client.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.client.close()

    def call(self, method: str, params: dict) -> dict:
        self.counter += 1
        response = self.client.request(method, params, request_id=f"e2e_{self.counter}")
        assert response["request_id"] == f"e2e_{self.counter}", response
        return response

    def ok(self, method: str, params: dict) -> dict:
        response = self.call(method, params)
        assert response["ok"] is True, response
        return response["result"]

    def error(self, method: str, params: dict) -> str:
        response = self.call(method, params)
        assert response["ok"] is False, response
        return response["error"]["code"]

    def wait_for(self, task_id: str, statuses: set[str], timeout: float = 25.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.ok("task.get", {"task_id": task_id})
            if last["status"] in statuses:
                return last
            time.sleep(0.02)
        raise AssertionError(f"task {task_id} did not reach {statuses}: {last}")

    def submit(self, prompt: str, **extra: object) -> str:
        params: dict[str, object] = {
            # The runtime the Bridge answers for this driver: the unmapped production FakeAdapter
            # name, because a review profile is refused for a native runtime name on both
            # platforms. The MCP-surface driver uses the codex mapping instead.
            "runtime": "fake",
            "workdir": WORKDIR_ALIAS,
            "path": "",
            "profile": "review",
            "prompt": prompt,
        }
        params.update(extra)
        result = self.ok("task.submit", params)
        assert result["status"] == "queued", result
        return result["task_id"]


def test_discovery_and_the_review_workdir_policy(bridge: BridgeProcess) -> None:
    with Session(bridge.client()) as session:
        runtimes = session.ok("runtime.list", {})
        assert [item["name"] for item in runtimes["runtimes"]] == ["fake"]
        models = session.ok("runtime.models", {"runtime": "fake"})
        assert models["status"] == "unsupported"
        # This fixture serves a review workdir, because the writer lease is Phase C: the policy
        # layer refuses a write profile here, and the lease seam's own fail-closed answer —
        # BRIDGE_PLATFORM_UNSUPPORTED through the same pipe — is proven in
        # tests/test_windows_mcp_agent_e2e.py::test_submit_reaches_the_bridge_and_fails_closed…
        assert (
            session.error(
                "task.submit",
                {
                    "runtime": "fake",
                    "workdir": WORKDIR_ALIAS,
                    "path": "",
                    "profile": "workspace-write",
                    "prompt": "complete: must not run",
                },
            )
            == "AGENT_PROFILE_NOT_ALLOWED"
        )
        assert session.error("task.get", {"task_id": "agt_absent"}) == "AGENT_TASK_NOT_FOUND"


def test_completed_task_events_and_inline_result(bridge: BridgeProcess) -> None:
    with Session(bridge.client()) as session:
        task_id = session.submit("complete: hello over the Windows pipe")
        settled = session.wait_for(task_id, {"succeeded"})
        assert settled["final_response"] == "hello over the Windows pipe"
        events = session.ok("task.events", {"task_id": task_id, "limit": 50})
        assert any(event["event_type"] == "task.completed" for event in events["events"])
        # Under the harness' lowered inline bound a short answer stays inline, and the spool
        # reader says so with the frozen code instead of inventing an empty file.
        assert session.error("task.result.read", {"task_id": task_id}) == (
            "AGENT_RESULT_NOT_RETRIEVABLE"
        )
        assert (bridge.state_dir / "state.sqlite3").exists()


def test_spooled_result_is_read_back_in_chunks_over_the_pipe(bridge: BridgeProcess) -> None:
    text = "spool" * 2_000
    with Session(bridge.client()) as session:
        task_id = session.submit(f"complete:{text}")
        session.wait_for(task_id, {"succeeded"})
        offset = 0
        received = ""
        reads = 0
        while True:
            chunk = session.ok(
                "task.result.read",
                {"task_id": task_id, "offset_bytes": offset, "max_bytes": 8_192},
            )
            received += chunk["text"]
            offset = chunk["next_offset_bytes"]
            reads += 1
            assert offset > 0 or chunk["eof"]
            if chunk["eof"]:
                break
        assert received == text
        assert reads > 1, reads
        assert (bridge.state_dir / "results" / f"{task_id}.txt").exists()


def test_large_response_frame_survives_the_pipe(bridge: BridgeProcess) -> None:
    """A response far bigger than one 64 KiB read arrives whole and in one frame."""
    text = "x" * 60_000
    with Session(bridge.client()) as session:
        task_id = session.submit(f"complete:{text}")
        session.wait_for(task_id, {"succeeded"})
        collected = ""
        offset = 0
        while True:
            chunk = session.ok(
                "task.result.read",
                {"task_id": task_id, "offset_bytes": offset, "max_bytes": 65_536},
            )
            collected += chunk["text"]
            offset = chunk["next_offset_bytes"]
            assert len(chunk["text"].encode("utf-8")) <= MAX_RESPONSE_BYTES
            if chunk["eof"]:
                break
        assert collected == text


def test_approval_question_message_and_cancel(bridge: BridgeProcess) -> None:
    with Session(bridge.client()) as session:
        approval_id = session.submit("approval: echo windows")
        waiting = session.wait_for(approval_id, {"waiting_for_approval"})
        request = waiting["pending_request"]
        assert request["kind"] == "approval"
        resolved = session.ok(
            "task.approval.respond",
            {
                "task_id": approval_id,
                "request_id": request["request_id"],
                "decision": "approve_once",
            },
        )
        assert resolved["resolved"] is True
        approved = session.wait_for(approval_id, TERMINAL)
        assert approved["status"] == "succeeded"
        assert "approve_once" in approved["final_response"]
        assert session.error(
            "task.approval.respond",
            {
                "task_id": approval_id,
                "request_id": request["request_id"],
                "decision": "approve_once",
            },
        ) in {"REQUEST_ALREADY_RESOLVED", "REQUEST_STALE"}

        question_id = session.submit("question: Which layer?")
        asking = session.wait_for(question_id, {"waiting_for_question"})
        question = asking["pending_request"]
        answered = session.ok(
            "task.question.answer",
            {
                "task_id": question_id,
                "request_id": question["request_id"],
                "answers": [
                    {"question_id": "q1", "selected_option_ids": ["b"], "text": "prefer B"}
                ],
            },
        )
        assert answered["resolved"] is True
        assert session.wait_for(question_id, TERMINAL)["status"] == "succeeded"

        steered = session.submit("steer: start working")
        session.wait_for(steered, {"running"})
        session.ok("task.message.send", {"task_id": steered, "message": "steered windows"})
        settled = session.wait_for(steered, TERMINAL)
        assert settled["final_response"] == "steered=steered windows"

        waiting_task = session.submit("wait: never finishes")
        session.wait_for(waiting_task, {"running"})
        session.ok("task.cancel", {"task_id": waiting_task})
        assert session.wait_for(waiting_task, TERMINAL)["status"] in {
            "cancelled",
            "interrupted",
        }


def test_idempotent_submission_returns_the_same_task(bridge: BridgeProcess) -> None:
    with Session(bridge.client()) as session:
        first = session.submit(
            "complete: once only", idempotency_key="windows-e2e-key", correlation_id="corr-1"
        )
        second = session.ok(
            "task.submit",
            {
                "runtime": "fake",
                "workdir": WORKDIR_ALIAS,
                "path": "",
                "profile": "review",
                "prompt": "complete: once only",
                "idempotency_key": "windows-e2e-key",
                "correlation_id": "corr-1",
            },
        )
        assert second["task_id"] == first
        session.wait_for(first, TERMINAL)


def test_many_sequential_clients_and_tasks(bridge: BridgeProcess) -> None:
    """The instance pool keeps serving after every connection, across several tasks."""
    for round_index in range(6):
        with Session(bridge.client()) as session:
            task_id = session.submit(f"complete: round {round_index}")
            settled = session.wait_for(task_id, {"succeeded"})
            assert settled["final_response"] == f"round {round_index}"
    assert (bridge.state_dir / "state.sqlite3").exists()
    # Everything the subprocess wrote stays inside the state tree it created.
    assert {path.name for path in bridge.state_dir.iterdir()} >= {"state.sqlite3", "results"}

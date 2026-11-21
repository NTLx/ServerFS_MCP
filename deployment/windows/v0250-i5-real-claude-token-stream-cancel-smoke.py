"""One genuine Claude SDK text-delta cancel gate, disposable Windows I5 only.

Reuses prior audited I5 protected start/stop and private owner proof. Requires
a durable top-level text delta while RUNNING before cancellation; never exposes
model text, task identifiers, credentials or prompts. Exactly one Provider
submission. No tools, commands, files, approvals, DNS or Tunnel changes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

PHASE = "entry"
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


class I5RealStreamError(RuntimeError):
    """Only fixed stage and exception type appear in public error output."""


def require(value: bool, reason: str) -> None:
    if not value:
        raise I5RealStreamError(reason)


def _helper(filename: str):
    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location("i5_real_stream_parent_gate", source)
    require(spec is not None and spec.loader is not None, "existing private gate unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def run_delta_cancel(bindings: dict, keys: dict[str, str], real) -> str:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from serverfs_mcp.agent_client import AgentBridgeRemoteError

    _, a = bindings["i5-core-a"].build_wiring()
    _, b = bindings["i5-core-b"].build_wiring()
    task_id: str | None = None
    cancelled = False
    try:
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + keys["i5-core-a"]},
                trust_env=False,
                timeout=25,
            ) as http,
            streamable_http_client("http://127.0.0.1:18873/mcp", http_client=http) as (read, write),
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                reply = await session.call_tool(
                    "submit_agent_task",
                    {
                        "runtime": "claude",
                        "workdir": "Fixture",
                        "path": "",
                        "profile": "workspace-write",
                        "prompt": (
                            "This is a text-only output streaming test. Start responding "
                            "immediately with a numbered list. Write at least 250 detailed "
                            "explanations of general software quality practices, each "
                            "several sentences long. Output plain text ONLY. "
                            "Do NOT access files, execute commands, invoke tools, browse, "
                            "use MCP, request permissions or ask questions. "
                            "No project context or external data is needed."
                        ),
                    },
                )
                task_id = real.result_mapping(reply).get("task_id")
                require(
                    isinstance(task_id, str) and 4 <= len(task_id) <= 128,
                    "http-task-id-invalid",
                )
        print("I5_STREAM_SUBMITTED_CORE_A_HTTP=PASS")

        seen_start = False
        seen_delta = False
        cursor = 0
        deadline = time.monotonic() + 65
        while time.monotonic() < deadline:
            task = await a.call("task.get", {"workdir": "Fixture", "task_id": task_id})
            status = task.get("status")
            require(isinstance(status, str), "task-status-invalid")
            require(
                status not in TERMINAL
                and status not in {"waiting_for_approval", "waiting_for_question"},
                "real-provider-ended-or-requested-interaction-before-delta",
            )
            page = await a.call(
                "task.events",
                {
                    "workdir": "Fixture",
                    "task_id": task_id,
                    "after_event_id": cursor,
                    "limit": 100,
                },
            )
            events = page.get("events")
            real.assert_no_unsafe_event(events)
            for event in events:
                if event.get("event_type") == "task.started":
                    seen_start = True
                if event.get("event_type") == "agent.text_delta":
                    payload = event.get("payload")
                    require(
                        isinstance(payload, dict)
                        and isinstance(payload.get("text"), str)
                        and bool(payload["text"])
                        and type(payload.get("index")) is int,
                        "text-delta-payload-invalid",
                    )
                    seen_delta = True
                event_id = event.get("event_id")
                require(
                    type(event_id) is int and event_id > cursor,
                    "event-cursor-invalid",
                )
                cursor = event_id
            if seen_start and seen_delta and status == "running":
                break
            await asyncio.sleep(0.15)
        require(
            seen_start and seen_delta and status == "running",
            "genuine-provider-text-delta-not-observed-inflight",
        )
        print("I5_REAL_PROVIDER_TEXT_DELTA_RUNNING=PASS")

        for method in ("task.get", "task.events", "task.result.read", "task.cancel"):
            params: dict = {"workdir": "Fixture", "task_id": task_id}
            if method == "task.events":
                params.update(after_event_id=0, limit=25)
            if method == "task.result.read":
                params.update(offset_bytes=0, max_bytes=256)
            try:
                await b.call(method, params)
            except AgentBridgeRemoteError as exc:
                require(exc.code == "CORE_TASK_NOT_FOUND", "foreign-core-denial-drift")
            else:
                raise I5RealStreamError("foreign-core-access-not-denied")
        print("I5_REAL_STREAM_FOREIGN_CORE_FOUR_METHODS=DENIED")

        state = await a.call("task.get", {"workdir": "Fixture", "task_id": task_id})
        require(state.get("status") == "running", "provider-finished-before-cancel")
        outcome = await asyncio.wait_for(
            a.call("task.cancel", {"workdir": "Fixture", "task_id": task_id}),
            timeout=14,
        )
        require(outcome.get("status") == "cancelled", "owner-cancel-unacknowledged")
        for _ in range(100):
            state = await a.call("task.get", {"workdir": "Fixture", "task_id": task_id})
            if state.get("status") == "cancelled":
                cancelled = True
                break
            await asyncio.sleep(0.15)
        require(cancelled, "cancel-terminal-not-persisted")
        require(state.get("pending_request") is None, "cancel-pending-request-not-absent")
        print("I5_REAL_STREAM_OWNER_CANCELLED=PASS")

        audit = await a.call(
            "task.events",
            {"workdir": "Fixture", "task_id": task_id, "after_event_id": 0, "limit": 500},
        )
        events = audit.get("events")
        real.assert_no_unsafe_event(events)
        delta_ids = [
            row["event_id"] for row in events if row.get("event_type") == "agent.text_delta"
        ]
        cancel_ids = [
            row["event_id"] for row in events if row.get("event_type") == "task.cancelled"
        ]
        require(
            bool(delta_ids)
            and len(cancel_ids) == 1
            and min(delta_ids) < cancel_ids[0]
            and not any(row.get("event_type") == "task.completed" for row in events),
            "stream-cancel-events-out-of-order",
        )
        print("I5_REAL_STREAM_CANCEL_EVENT_ORDER=PASS")
        return task_id
    finally:
        if task_id is not None and not cancelled:
            try:
                await asyncio.wait_for(
                    a.call("task.cancel", {"workdir": "Fixture", "task_id": task_id}),
                    timeout=12,
                )
                print("I5_REAL_STREAM_FINALLY_CANCEL=REQUESTED")
            except Exception:
                print("I5_REAL_STREAM_FINALLY_CANCEL=UNVERIFIED")


def prove_stream_offline(task_id: str) -> None:
    from serverfs_mcp.windows_v25 import storage

    worker = storage.root("i5-shared") / "envs/agent-bridge/Scripts/python.exe"
    script = Path(__file__).with_name("v0250-i5-real-stream-proof-worker.py")
    require(
        worker.is_file() and not worker.is_symlink() and script.is_file(),
        "installed-private-stream-proof-unavailable",
    )
    require(
        isinstance(task_id, str) and len(task_id) == 28 and task_id.isascii(),
        "private-id-shape-invalid",
    )
    result = subprocess.run(
        [str(worker), "-I", "-B", str(script)],
        input=task_id.encode("ascii"),
        capture_output=True,
        check=False,
        timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    expected = b"I5_REAL_STREAM_PRIVATE_TEXT_BEFORE_CANCEL=PASS"
    require(
        result.returncode == 0 and result.stdout.strip() == expected and not result.stderr,
        "independent-private-stream-proof-failed",
    )
    print(expected.decode("ascii"))


def main() -> None:
    global PHASE
    require(sys.argv[1:] == ["--run-once"], "explicit-one-shot-guard-required")
    old = _helper("v0250-i5-real-claude-inflight-cancel-smoke.py")
    old_proof = old.prove_native_session_after_stop

    def both_proofs(task_id: str) -> None:
        old_proof(task_id)
        prove_stream_offline(task_id)

    # Keep old.main() protected production/fixture owner start, exact grants,
    # HTTP tokens, three-fixture reverse stop, generation checks and SDK init
    # proof. Replace only the stronger real streamed-delta test and proof.
    old.run_cancel = run_delta_cancel
    old.prove_native_session_after_stop = both_proofs
    PHASE = "one-provider-turn-stream-cancel-and-protected-cleanup"
    old.main()
    print("I5_REAL_PROVIDER_TOKEN_STREAM_CANCEL=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no provider text or private data output
        print(
            "I5_REAL_STREAM_CANCEL=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

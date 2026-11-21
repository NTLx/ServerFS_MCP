"""One-use installed I5 Claude in-flight cancellation and Core-ownership gate.

Only three previously installed, Disabled, disposable I5 Scheduler tasks may
start. Exactly one real Claude task is submitted through Core A HTTP MCP and
cancelled by owner A after a 'running' + task.started observation. Core B
cannot read or cancel it. If the provider finishes before the race can be
measured, return INCONCLUSIVE as a failure, never a false PASS.

A real Claude SDK may consume the logged-in user's Provider quota/session.
No file access, tools, repo ops, interactive prompts or public Tunnel may be
approved. No raw model output, private task IDs, credentials or paths printed.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import time
from pathlib import Path

BRIDGE = "i5-shared"
CORES = {"i5-core-a": 18873, "i5-core-b": 18874}
PHASE = "entry"
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


class I5CancelGateError(RuntimeError):
    """Only stable, sanitized failure class may leave the process."""


def require(condition: bool, stage: str) -> None:
    if not condition:
        raise I5CancelGateError(stage)


def helper(filename: str, name: str):
    import importlib.util

    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, source)
    require(spec is not None and spec.loader is not None, "fixed-helper-unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def run_cancel(bindings: dict, keys: dict[str, str], real) -> str:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from serverfs_mcp.agent_client import AgentBridgeRemoteError

    _, a_client = bindings["i5-core-a"].build_wiring()
    _, b_client = bindings["i5-core-b"].build_wiring()
    task_id: str | None = None
    cancelled = False
    try:
        # Only one submission; the long text-only response creates an
        # opportunity to measure active-task cancellation without tool calls.
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + keys["i5-core-a"]},
                trust_env=False,
                timeout=25,
            ) as http,
            streamable_http_client(
                f"http://127.0.0.1:{CORES['i5-core-a']}/mcp", http_client=http
            ) as (read, write),
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
                            "This is a text-only concurrency and cancellation test. "
                            "Write a detailed numbered explanatory essay with at least "
                            "150 separate entries, each several sentences long, about "
                            "general software quality principles. Do not access files, "
                            "execute commands, use tools, browse, write anything, "
                            "call MCP, or request permissions. Output text only."
                        ),
                    },
                )
                submitted = real.result_mapping(reply)
                task_id = submitted.get("task_id")
                require(
                    isinstance(task_id, str) and 4 <= len(task_id) <= 128,
                    "http-task-id-invalid",
                )
                print("I5_CANCEL_SUBMITTED_VIA_CORE_A_HTTP=PASS")

        # Need both the Bridge running state and durable task.started event.
        # A queued cancellation is insufficient evidence of in-flight cancel.
        observed_running = False
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            current = await a_client.call("task.get", {"workdir": "Fixture", "task_id": task_id})
            status = current.get("status")
            require(isinstance(status, str), "task-status-invalid")
            if status in TERMINAL:
                raise I5CancelGateError("provider-finished-before-running-probe")
            require(
                status not in ("waiting_for_approval", "waiting_for_question"),
                "provider-unexpected-interaction",
            )
            if status == "running":
                event_page = await a_client.call(
                    "task.events",
                    {
                        "workdir": "Fixture",
                        "task_id": task_id,
                        "after_event_id": 0,
                        "limit": 100,
                    },
                )
                events = event_page.get("events")
                real.assert_no_unsafe_event(events)
                if any(event.get("event_type") == "task.started" for event in events):
                    observed_running = True
                    break
            await asyncio.sleep(0.15)
        require(observed_running, "running-and-start-event-not-observed")
        print("I5_CANCEL_BRIDGE_TASK_RUNNING_AND_STARTED=PASS")

        # Bridge task.started is emitted before the Claude SDK handshake.
        # Keep the running task alive long enough to permit SDK init and
        # durable session persistence; never assert provider in-flight from
        # the Bridge start event alone.
        warmup_deadline = time.monotonic() + 8.0
        while time.monotonic() < warmup_deadline:
            current = await a_client.call("task.get", {"workdir": "Fixture", "task_id": task_id})
            require(current.get("status") == "running", "provider-finished-during-warmup")
            require(
                current.get("pending_request") is None,
                "provider-interaction-during-warmup",
            )
            events_page = await a_client.call(
                "task.events",
                {"workdir": "Fixture", "task_id": task_id, "after_event_id": 0, "limit": 100},
            )
            real.assert_no_unsafe_event(events_page.get("events"))
            await asyncio.sleep(0.25)
        print("I5_CANCEL_RUNNING_WARMUP=PASS")

        for method in ("task.get", "task.events", "task.result.read", "task.cancel"):
            params: dict = {"workdir": "Fixture", "task_id": task_id}
            if method == "task.events":
                params.update(after_event_id=0, limit=25)
            if method == "task.result.read":
                params.update(offset_bytes=0, max_bytes=256)
            try:
                await b_client.call(method, params)
            except AgentBridgeRemoteError as exc:
                require(exc.code == "CORE_TASK_NOT_FOUND", "cross-core-denial-drift")
            else:
                raise I5CancelGateError("foreign-core-task-exposed")
        print("I5_CANCEL_FOREIGN_CORE_FOUR_METHODS=DENIED")

        # Close the race explicitly: the task must still be running directly
        # before authorized cancel, or the result is inconclusive.
        before = await a_client.call("task.get", {"workdir": "Fixture", "task_id": task_id})
        require(before.get("status") == "running", "not-running-before-cancel")
        outcome = await asyncio.wait_for(
            a_client.call("task.cancel", {"workdir": "Fixture", "task_id": task_id}),
            timeout=14,
        )
        require(outcome.get("status") == "cancelled", "owner-cancel-not-acknowledged")
        # Cancellation result can lead persistent terminal transition by
        # one scheduling tick, so poll boundedly.
        deadline = time.monotonic() + 16
        while time.monotonic() < deadline:
            task = await a_client.call("task.get", {"workdir": "Fixture", "task_id": task_id})
            if task.get("status") == "cancelled":
                cancelled = True
                break
            require(
                task.get("status") not in {"succeeded", "failed", "interrupted"},
                "cancel-transition-unexpected-terminal",
            )
            await asyncio.sleep(0.15)
        require(cancelled, "cancel-terminal-not-proven")
        require(task.get("pending_request") is None, "cancel-pending-interaction")
        print("I5_CANCEL_OWNING_CORE_TERMINAL_CANCELLED=PASS")

        page = await a_client.call(
            "task.events",
            {
                "workdir": "Fixture",
                "task_id": task_id,
                "after_event_id": 0,
                "limit": 100,
            },
        )
        events = page.get("events")
        real.assert_no_unsafe_event(events)
        kinds = {event["event_type"] for event in events}
        require("task.started" in kinds, "start-event-missing")
        require("task.cancelled" in kinds, "cancel-event-missing")
        require("task.completed" not in kinds, "completed-after-cancel")
        print("I5_CANCEL_PRIVATE_EVENT_AUDIT=PASS")
        return task_id
    finally:
        if task_id is not None and not cancelled:
            try:
                await asyncio.wait_for(
                    a_client.call("task.cancel", {"workdir": "Fixture", "task_id": task_id}),
                    timeout=12,
                )
                print("I5_CANCEL_FINALLY_OWNER_CANCEL=REQUESTED")
            except Exception:
                print("I5_CANCEL_FINALLY_OWNER_CANCEL=UNVERIFIED")


def prove_native_session_after_stop(task_id: str) -> None:
    """Attest exact cancelled task via the separately installed Bridge worker.

    The owner Core Python intentionally lacks serverfs_agent_bridge: its
    isolated environment cannot import the worker's ACL verification module.
    Use a fixed worker entrypoint with the task ID on stdin (never argv).
    Suppress all worker output except one exact non-secret PASS marker.
    """
    import subprocess

    from serverfs_mcp.windows_v25 import storage

    worker = storage.root(BRIDGE) / "envs" / "agent-bridge" / "Scripts" / "python.exe"
    proof_script = Path(__file__).with_name("v0250-i5-cancel-state-proof-worker.py")
    require(
        worker.is_file() and not worker.is_symlink() and proof_script.is_file(),
        "installed-private-proof-worker-unavailable",
    )
    require(
        isinstance(task_id, str) and len(task_id) == 28 and task_id.isascii(),
        "private-task-id-shape-invalid",
    )
    response = subprocess.run(
        [str(worker), "-I", "-B", str(proof_script)],
        input=task_id.encode("ascii"),
        capture_output=True,
        check=False,
        timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    marker = b"I5_CANCEL_PRIVATE_CLAUDE_INIT_AND_DURABLE_OWNER=PASS"
    require(
        response.returncode == 0 and response.stdout.strip() == marker and not response.stderr,
        "private-bridge-worker-proof-not-accepted",
    )
    print(marker.decode("ascii"))


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(sys.argv[1:] == ["--run-once"], "explicit-one-shot-required")
    PHASE = "native-installed-owner"
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "non-elevated-isolated-owner-required",
    )
    expected = storage.root(BRIDGE) / "envs" / "bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "installed-bridge-owner-interpreter-required",
    )
    real = helper("v0250-i5-real-claude-task-ownership-smoke.py", "i5_real_provider_helper")
    smoke = helper("v0250-i5-provider-installed-smoke.py", "i5_no_model_helper")
    gate = smoke.load_gate()
    PHASE = "protected-host-baseline"
    baseline = smoke.protected_snapshot(gate)
    smoke.candidates_disabled(gate)
    prod = storage.load("core")
    generation = _current_identity(prod)
    require(generation is not None, "production-generation-unknown")

    PHASE = "fixed-fixture-grants-and-auth"
    sid = current_user_sid()
    fixture = storage.root(BRIDGE) / "workdirs" / "fixture"
    storage.check_private(fixture, directory=True, sid=sid)
    require(fixture.is_dir() and not fixture.is_symlink(), "fixture-invalid")
    bindings: dict = {}
    keys: dict[str, str] = {}
    for core, port in CORES.items():
        manifest = storage.load("core", core)
        require(
            manifest.instance_id == core and manifest.settings["port"] == port,
            "core-identity-port-drift",
        )
        workdirs, policy = load_native_config(Path(str(manifest.settings["config"])))
        require(
            policy.bridge_required
            and len(workdirs) == 1
            and workdirs[0].alias == "Fixture"
            and workdirs[0].root.resolve(strict=True) == fixture.resolve(strict=True)
            and workdirs[0].agent_runtimes == frozenset({"claude"})
            and not workdirs[0].read_only,
            "fixture-workdir-policy-drift",
        )
        binding = load_native_core_bridge_binding(manifest, workdirs)
        require(
            binding.bridge_mode == "shared"
            and binding.bridge_id == BRIDGE
            and binding.instance_id == core,
            "signed-core-scope-drift",
        )
        bindings[core] = binding
        auth = storage.home("core", core) / "http.env"
        storage.check_private(auth, directory=False, sid=sid)
        token = _read_env_file(auth).get("SERVERFS_HTTP_API_KEY")
        require(isinstance(token, str) and len(token) >= 40, "auth-key-invalid")
        keys[core] = token
    require(len(set(keys.values())) == 2, "core-http-auth-not-independent")

    started: list[tuple[str, str]] = []
    failure: BaseException | None = None
    failed_phase = ""
    cleanup = False
    verified_task_id: str | None = None
    try:
        PHASE = "start-private-i5-bridge"
        started.append(("bridge", BRIDGE))
        require(
            smoke.cli("bridge", BRIDGE, "start").get("readiness") == "owner_alive",
            "bridge-owner-not-ready",
        )
        for core in CORES:
            PHASE = "start-private-" + core
            started.append(("core", core))
            require(
                smoke.cli("core", core, "start").get("readiness") == "verified",
                "core-not-ready",
            )
        PHASE = "single-real-provider-running-cancel"
        verified_task_id = asyncio.run(
            asyncio.wait_for(run_cancel(bindings, keys, real), timeout=120)
        )
    except BaseException as exc:
        failure, failed_phase = exc, PHASE
    finally:
        PHASE = "reverse-stop-three-i5-fixtures"
        cleanup = True
        for component, instance in reversed(started):
            try:
                row = smoke.cli(component, instance, "stop")
                cleanup = cleanup and (
                    row.get("state") == "Disabled"
                    and row.get("enabled") is False
                    and row.get("running") is False
                )
            except Exception:
                cleanup = False
        try:
            smoke.candidates_disabled(gate)
        except Exception:
            cleanup = False
    print("I5_CANCEL_THREE_FIXTURES_STOP=" + ("VERIFIED" if cleanup else "UNVERIFIED"))
    require(cleanup, "fixture-cleanup-not-proven")
    PHASE = "postflight-protected-host"
    require(
        smoke.protected_snapshot(gate) == baseline,
        "protected-task-baseline-drift",
    )
    after = _current_identity(prod)
    require(
        after is not None
        and after.pid == generation.pid
        and after.creation_time == generation.creation_time,
        "production-generation-drift",
    )
    print("I5_CANCEL_PROTECTED_PRODUCTION_AND_I1_I4=UNCHANGED")
    if failure is not None:
        PHASE = failed_phase
        raise failure
    PHASE = "private-offline-provider-init-and-owner-proof"
    require(verified_task_id is not None, "cancelled-task-id-unavailable")
    prove_native_session_after_stop(verified_task_id)
    print("I5_REAL_PROVIDER_INFLIGHT_OWNER_CANCEL=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 — no model/provider contents in logs
        print(
            "I5_REAL_PROVIDER_INFLIGHT_CANCEL=FAILED phase="
            + PHASE
            + " type="
            + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

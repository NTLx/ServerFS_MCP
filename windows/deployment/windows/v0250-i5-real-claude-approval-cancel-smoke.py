"""Genuine installed I5 Claude permission-approval pending/cancel canary.

One exact side-effect-free Python print command; no file edits.
This test must observe a real SDK can_use_tool approval awaiting an answer.
No user approval is granted: verify foreign Core denial, owner cancel, late
reply stale, independent SQLite terminal record, all three I5 services stop.
Does not claim a successful shell command executed. Any native auto-allow,
provider refusal, unexpected tool or absence of pending approval is FAIL.
No Provider text, private payloads, identities or credentials are printed.
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
TERMINAL = frozenset({"succeeded", "cancelled", "failed", "interrupted"})
SAFE_APPROVAL_COMMAND = "python -I -B -c \"print('SERVERFS_I5_APPROVAL_PROBE')\""


class I5RealApprovalError(RuntimeError):
    """Fail closed; only stable stage/class may escape."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5RealApprovalError(reason)


def helper(filename: str, label: str):
    import importlib.util

    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "fixed helper missing")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def safe_events(events: object) -> set[str]:
    require(isinstance(events, list), "approval event list invalid")
    kinds: set[str] = set()
    for event in events:
        require(isinstance(event, dict), "approval event invalid")
        kind = event.get("event_type")
        require(isinstance(kind, str), "approval event kind invalid")
        if kind == "command.started":
            # A Claude ToolUse announcement, *not proof the command executed*.
            payload = event.get("payload")
            require(
                isinstance(payload, dict) and payload.get("command") == SAFE_APPROVAL_COMMAND,
                "unexpected proposed tool command",
            )
        require(
            kind
            not in {
                "command.completed",
                "file.changed",
                "file_change.started",
                "tool.completed",
                "approval.resolved",
                "task.completed",
            }
            and not kind.startswith("file_change."),
            "approval canary performed an unauthorized operation",
        )
        if kind == "tool.started":
            raise I5RealApprovalError("unexpected non-Bash tool")
        kinds.add(kind)
    return kinds


async def run_approval(bindings: dict, keys: dict[str, str], real) -> str:
    global PHASE

    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from serverfs_mcp.agent_client import AgentBridgeRemoteError

    _, owner = bindings["i5-core-a"].build_wiring()
    _, foreign = bindings["i5-core-b"].build_wiring()
    task_id: str | None = None
    cancelled = False
    try:
        PHASE = "genuine-provider-approval-submit"
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + keys["i5-core-a"]},
                trust_env=False,
                timeout=35,
            ) as http,
            streamable_http_client(
                f"http://127.0.0.1:{CORES['i5-core-a']}/mcp", http_client=http
            ) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            catalog = {tool.name for tool in (await session.list_tools()).tools}
            require(
                {"submit_agent_task", "get_agent_task", "cancel_agent_task"} <= catalog,
                "I5 installed HTTP MCP task interface unavailable",
            )
            result = await session.call_tool(
                "submit_agent_task",
                {
                    "runtime": "claude",
                    "workdir": "Fixture",
                    "path": "",
                    "profile": "workspace-write",
                    "prompt": (
                        "This is a disposable permission-callback test. "
                        "Use your built-in Bash tool to request EXACTLY this command, "
                        "spelled exactly as given: "
                        + SAFE_APPROVAL_COMMAND
                        + ". Wait for permission before execution. Do not use a built-in "
                        "AskUserQuestion, Read, Write, Edit, search, file access, MCP, "
                        "or any other tool; do not change files or settings. "
                        "Do not skip permission, do not ask permission in prose. "
                        "If permission is not required for this exact Python "
                        "print command, finish without attempting any other command."
                    ),
                },
            )
            submitted = real.result_mapping(result)
            task_id = submitted.get("task_id")
            require(
                isinstance(task_id, str) and 4 <= len(task_id) <= 128,
                "genuine provider approval submission lacked task ID",
            )
            print("I5_REAL_APPROVAL_SUBMITTED_VIA_CORE_A_HTTP=PASS")

        PHASE = "wait-for-genuine-SDK-approval"
        request_id: str | None = None
        deadline = time.monotonic() + 80
        while time.monotonic() < deadline:
            task = await owner.call("task.get", {"workdir": "Fixture", "task_id": task_id})
            state = task.get("status")
            require(isinstance(state, str), "provider approval status invalid")
            if state == "waiting_for_approval":
                request = task.get("pending_request")
                require(
                    isinstance(request, dict)
                    and request.get("kind") == "approval"
                    and request.get("status") == "pending",
                    "SDK approval pending record invalid",
                )
                candidate = request.get("request_id")
                require(
                    isinstance(candidate, str) and 4 <= len(candidate) <= 128,
                    "SDK approval durable identifier unavailable",
                )
                payload = request.get("payload")
                require(
                    isinstance(payload, dict)
                    and payload.get("tool") == "Bash"
                    and payload.get("category") == "command"
                    and isinstance(payload.get("tool_input"), dict)
                    and payload["tool_input"].get("command") == SAFE_APPROVAL_COMMAND,
                    "genuine approval tool/payload differs from safe echo probe",
                )
                request_id = candidate
                break
            if state in TERMINAL:
                raise I5RealApprovalError("Provider completed without genuine pending SDK approval")
            require(
                state != "waiting_for_question",
                "Provider chose a question rather than permission approval",
            )
            await asyncio.sleep(0.25)
        require(request_id is not None, "real Provider permission callback not observed")
        print("I5_REAL_SDK_PENDING_APPROVAL=VERIFIED")

        PHASE = "genuine-approval-event-and-no-execution"
        events = await owner.call(
            "task.events",
            {"workdir": "Fixture", "task_id": task_id, "after_event_id": 0, "limit": 100},
        )
        kinds = safe_events(events.get("events"))
        require(
            {"task.started", "approval.requested"} <= kinds and "task.cancelled" not in kinds,
            "genuine pending-approval event missing",
        )
        print("I5_REAL_APPROVAL_DURABLE_REQUEST_EVENT=PASS")

        response = {
            "workdir": "Fixture",
            "task_id": task_id,
            "request_id": request_id,
            "decision": "approve_once",
        }
        for method in (
            "task.get",
            "task.events",
            "task.result.read",
            "task.cancel",
            "task.approval.respond",
        ):
            PHASE = "foreign-Core-" + method.replace(".", "-")
            params: dict = {"workdir": "Fixture", "task_id": task_id}
            if method == "task.events":
                params.update(after_event_id=0, limit=30)
            elif method == "task.result.read":
                params.update(offset_bytes=0, max_bytes=256)
            elif method == "task.approval.respond":
                params = response
            try:
                await foreign.call(method, params)
            except AgentBridgeRemoteError as exc:
                require(exc.code == "CORE_TASK_NOT_FOUND", "foreign Core denial drift")
            else:
                raise I5RealApprovalError("foreign Core accessed owner approval task")
        print("I5_REAL_APPROVAL_FOREIGN_CORE_FIVE_METHODS=DENIED")

        PHASE = "owner-approval-waiting-before-cancel"
        still = await owner.call("task.get", {"workdir": "Fixture", "task_id": task_id})
        require(
            still.get("status") == "waiting_for_approval"
            and isinstance(still.get("pending_request"), dict),
            "owner approval not still pending before cancel",
        )
        print("I5_REAL_APPROVAL_WAITING_BEFORE_CANCEL=PASS")

        PHASE = "owner-approval-terminal-cancel"
        outcome = await asyncio.wait_for(
            owner.call("task.cancel", {"workdir": "Fixture", "task_id": task_id}),
            timeout=15,
        )
        require(outcome.get("status") == "cancelled", "owner cancel refused")
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            now = await owner.call("task.get", {"workdir": "Fixture", "task_id": task_id})
            if now.get("status") == "cancelled":
                require(
                    now.get("pending_request_id") is None and now.get("pending_request") is None,
                    "cancelled approval retained pending request",
                )
                cancelled = True
                break
            require(
                now.get("status") not in TERMINAL,
                "approval cancellation reached wrong terminal",
            )
            await asyncio.sleep(0.2)
        require(cancelled, "real approval terminal cancel not observed")
        print("I5_REAL_APPROVAL_OWNER_TERMINAL_CANCELLED=PASS")

        PHASE = "late-owner-approval-response"
        try:
            await owner.call("task.approval.respond", response)
        except AgentBridgeRemoteError as exc:
            require(exc.code == "REQUEST_STALE", "late approval response was not stale")
        else:
            raise I5RealApprovalError("late approval accepted after cancellation")
        print("I5_REAL_APPROVAL_LATE_OWNER_RESPONSE=REQUEST_STALE")

        PHASE = "final-approval-audit"
        final_events = await owner.call(
            "task.events",
            {"workdir": "Fixture", "task_id": task_id, "after_event_id": 0, "limit": 100},
        )
        final_kinds = safe_events(final_events.get("events"))
        require(
            {"approval.requested", "task.cancelled"} <= final_kinds
            and "approval.resolved" not in final_kinds
            and "task.completed" not in final_kinds
            and "command.completed" not in final_kinds,
            "cancelled SDK approval audit invalid",
        )
        print("I5_REAL_APPROVAL_NO_LATE_COMPLETION=PASS")
        return task_id
    finally:
        if task_id is not None and not cancelled:
            try:
                reply = await asyncio.wait_for(
                    owner.call("task.cancel", {"workdir": "Fixture", "task_id": task_id}),
                    timeout=12,
                )
                if reply.get("status") == "cancelled":
                    final = await asyncio.wait_for(
                        owner.call("task.get", {"workdir": "Fixture", "task_id": task_id}),
                        timeout=6,
                    )
                    if (
                        final.get("status") == "cancelled"
                        and final.get("pending_request_id") is None
                        and final.get("pending_request") is None
                    ):
                        print("I5_REAL_APPROVAL_FINALLY_OWNER_CANCEL=VERIFIED")
                    else:
                        print("I5_REAL_APPROVAL_FINALLY_OWNER_CANCEL=REQUESTED")
                else:
                    print("I5_REAL_APPROVAL_FINALLY_OWNER_CANCEL=REQUESTED")
            except Exception:
                print("I5_REAL_APPROVAL_FINALLY_OWNER_CANCEL=UNVERIFIED")


def prove_durable_after_stop(task_id: str) -> None:
    import subprocess

    from serverfs_mcp.windows_v25 import storage

    worker = storage.root(BRIDGE) / "envs/agent-bridge/Scripts/python.exe"
    proof_script = Path(__file__).with_name("v0250-i5-real-approval-proof-worker.py")
    require(
        worker.is_file() and not worker.is_symlink() and proof_script.is_file(),
        "private installed Worker proof unavailable",
    )
    require(len(task_id) == 28 and task_id.startswith("agt_"), "private task ID shape invalid")
    proof = subprocess.run(
        [str(worker), "-I", "-B", str(proof_script)],
        input=task_id.encode("ascii"),
        capture_output=True,
        check=False,
        timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    marker = b"I5_REAL_APPROVAL_PRIVATE_STALE_SQLITE=PASS"
    require(
        proof.returncode == 0 and proof.stdout.strip() == marker and not proof.stderr,
        "private cancelled question persistence proof refused",
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

    require(sys.argv[1:] == ["--run-once"], "real Provider approval one-shot required")
    PHASE = "native-installed-i5-Bridge-owner"
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native nonadmin -I -B required",
    )
    expected = storage.root(BRIDGE) / "envs/bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "installed I5 Bridge Owner interpreter required",
    )
    smoke = helper("v0250-i5-provider-installed-smoke.py", "i5_real_approval_native_lifecycle")
    real = helper("v0250-i5-real-claude-task-ownership-smoke.py", "i5_real_approval_http_mapping")
    gate = smoke.load_gate()
    baseline = smoke.protected_snapshot(gate)
    smoke.candidates_disabled(gate)
    production = storage.load("core")
    generation = _current_identity(production)
    require(generation is not None, "production generation unavailable")
    sid = current_user_sid()
    fixture = storage.root(BRIDGE) / "workdirs/fixture"
    storage.check_private(fixture, directory=True, sid=sid)
    bindings: dict = {}
    keys: dict[str, str] = {}
    for core, port in CORES.items():
        manifest = storage.load("core", core)
        require(
            manifest.component == "core"
            and manifest.instance_id == core
            and manifest.settings["port"] == port,
            "I5 fixed Core identity changed",
        )
        workdirs, policy = load_native_config(Path(str(manifest.settings["config"])))
        require(
            policy.bridge_required
            and len(workdirs) == 1
            and workdirs[0].alias == "Fixture"
            and workdirs[0].root.resolve(strict=True) == fixture.resolve(strict=True)
            and not workdirs[0].read_only
            and workdirs[0].agent_runtimes == frozenset({"claude"}),
            "private Fixture policy changed",
        )
        binding = load_native_core_bridge_binding(manifest, workdirs)
        require(
            binding.bridge_id == BRIDGE
            and binding.instance_id == core
            and binding.bridge_mode == "shared",
            "I5 signed shared bridge binding changed",
        )
        bindings[core] = binding
        auth = storage.home("core", core) / "http.env"
        storage.check_private(auth, directory=False, sid=sid)
        key = _read_env_file(auth).get("SERVERFS_HTTP_API_KEY")
        require(isinstance(key, str) and len(key) >= 40, "private Core Bearer invalid")
        keys[core] = key
    require(len(set(keys.values())) == 2, "I5 Core Bearers collided")
    started: list[tuple[str, str]] = []
    failed: BaseException | None = None
    failed_phase = ""
    result_id: str | None = None
    cleanup = True
    try:
        PHASE = "start-only-private-i5-Bridge"
        started.append(("bridge", BRIDGE))
        require(
            smoke.cli("bridge", BRIDGE, "start").get("readiness") == "owner_alive",
            "I5 Bridge Owner did not start",
        )
        for core in CORES:
            PHASE = "start-only-private-" + core
            started.append(("core", core))
            require(
                smoke.cli("core", core, "start").get("readiness") == "verified",
                "I5 Core did not start",
            )
        PHASE = "one-real-SDK-BashPermission-await-and-cancel"
        result_id = asyncio.run(asyncio.wait_for(run_approval(bindings, keys, real), timeout=140))
    except BaseException as exc:  # noqa: BLE001 - no task data escapes
        failed, failed_phase = exc, PHASE
    finally:
        PHASE = "reverse-stop-three-private-i5-components"
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
    print("I5_REAL_APPROVAL_THREE_FIXTURES_STOP=" + ("VERIFIED" if cleanup else "UNVERIFIED"))
    require(cleanup, "private I5 fixtures not cleanly stopped")
    PHASE = "protected-process-generation-and-task-snapshot"
    require(smoke.protected_snapshot(gate) == baseline, "protected task baseline changed")
    now = _current_identity(production)
    require(
        now is not None
        and now.pid == generation.pid
        and now.creation_time == generation.creation_time,
        "protected production Core generation changed",
    )
    print("I5_REAL_APPROVAL_PROTECTED_PRODUCTION=UNCHANGED")
    if failed is not None:
        PHASE = failed_phase
        raise failed
    require(result_id is not None, "real Provider approval task ID unavailable")
    PHASE = "independent-private-Worker-stale-request-proof"
    prove_durable_after_stop(result_id)
    print("I5_REAL_PROVIDER_PENDING_APPROVAL_CANCEL_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no raw Provider or private paths
        print(
            "I5_REAL_APPROVAL=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

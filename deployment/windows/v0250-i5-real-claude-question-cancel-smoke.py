"""One genuine installed I5 Claude SDK AskUserQuestion pending/cancel probe.

One harmless fictional choice; NO external tools, file access, shell, Repo Ops,
Cloudflare, approvals or real content. The authentic Claude SDK (not the
deterministic fake) must call native AskUserQuestion, persist the pending
question, allow only owning Core A to cancel, mark the request stale, and
deny foreign Core B or late replies. If the Provider does not issue this
interaction, the one-shot canary is INCONCLUSIVE/FAIL, not a fake PASS.

Only three private I5 native Task Scheduler instances run and are stopped
in finally. Production Core PID+birthtime and other fixtures are protected.
No task IDs, prompts, provider text, question payload, keys or paths printed.
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


class I5RealQuestionError(RuntimeError):
    """Fail closed; only stable stage/class may escape."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5RealQuestionError(reason)


def helper(filename: str, label: str):
    import importlib.util

    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "fixed helper missing")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def safe_events(events: object) -> set[str]:
    require(isinstance(events, list), "pending events shape invalid")
    kinds: set[str] = set()
    for event in events:
        require(isinstance(event, dict), "pending event not mapping")
        kind = event.get("event_type")
        require(isinstance(kind, str), "pending event type invalid")
        payload = event.get("payload")
        require(
            not kind.startswith(("command.", "file_change."))
            and kind not in {"file.changed", "task.completed", "approval.resolved"},
            "question canary emitted forbidden activity",
        )
        if kind.startswith("tool."):
            require(
                kind == "tool.started"
                and isinstance(payload, dict)
                and payload.get("tool") == "AskUserQuestion",
                "real Provider used unexpected tool",
            )
        kinds.add(kind)
    return kinds


async def run_question(bindings: dict, keys: dict[str, str], real) -> str:
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
                "installed I5 question client MCP capability unavailable",
            )
            result = await session.call_tool(
                "submit_agent_task",
                {
                    "runtime": "claude",
                    "workdir": "Fixture",
                    "path": "",
                    "profile": "workspace-write",
                    "prompt": (
                        "This is a nonproduction interactive-question test. "
                        "Use your BUILT-IN AskUserQuestion tool now to ask ONE "
                        "fictional multiple-choice question: "
                        "'Which sample label should be used: Alpha or Beta?' "
                        "Give exactly two options Alpha and Beta, with brief "
                        "descriptions. Wait for the user to answer; do not "
                        "choose an answer yourself. Do NOT use Bash, Read, "
                        "Write, Edit, shell commands, search, MCP tools, "
                        "file access, approvals or other tools. "
                        "Do not change anything. Only use AskUserQuestion."
                    ),
                },
            )
            submitted = real.result_mapping(result)
            task_id = submitted.get("task_id")
            require(
                isinstance(task_id, str) and 4 <= len(task_id) <= 128,
                "provider question submission lacked task ID",
            )
            print("I5_REAL_QUESTION_SUBMITTED_VIA_CORE_A_HTTP=PASS")

        request_id: str | None = None
        deadline = time.monotonic() + 80.0
        while time.monotonic() < deadline:
            task = await owner.call("task.get", {"workdir": "Fixture", "task_id": task_id})
            state = task.get("status")
            require(isinstance(state, str), "provider question status missing")
            if state == "waiting_for_question":
                request = task.get("pending_request")
                require(
                    isinstance(request, dict)
                    and request.get("kind") == "question"
                    and request.get("status") == "pending",
                    "pending SDK question details invalid",
                )
                candidate = request.get("request_id")
                require(
                    isinstance(candidate, str) and 4 <= len(candidate) <= 128,
                    "durable pending request ID absent",
                )
                request_id = candidate
                break
            if state in TERMINAL:
                raise I5RealQuestionError("provider finished without genuine pending SDK question")
            require(state != "waiting_for_approval", "provider attempted unrequested approval")
            await asyncio.sleep(0.25)
        require(request_id is not None, "real Provider AskUserQuestion not observed")
        print("I5_REAL_SDK_PENDING_QUESTION=VERIFIED")

        events_row = await owner.call(
            "task.events",
            {"workdir": "Fixture", "task_id": task_id, "after_event_id": 0, "limit": 100},
        )
        kinds = safe_events(events_row.get("events"))
        require(
            {"task.started", "question.requested"} <= kinds and "task.cancelled" not in kinds,
            "real SDK pending-question events absent",
        )
        print("I5_REAL_QUESTION_DURABLE_REQUEST_EVENT=PASS")

        answers = [{"question_id": "q0", "selected_option_ids": ["Alpha"]}]
        response = {
            "workdir": "Fixture",
            "task_id": task_id,
            "request_id": request_id,
            "answers": answers,
        }
        for method in (
            "task.get",
            "task.events",
            "task.result.read",
            "task.cancel",
            "task.question.answer",
        ):
            PHASE = "foreign-Core-" + method.replace(".", "-")
            params: dict = {"workdir": "Fixture", "task_id": task_id}
            if method == "task.events":
                params.update(after_event_id=0, limit=30)
            elif method == "task.result.read":
                params.update(offset_bytes=0, max_bytes=256)
            elif method == "task.question.answer":
                params = response
            try:
                await foreign.call(method, params)
            except AgentBridgeRemoteError as exc:
                require(exc.code == "CORE_TASK_NOT_FOUND", "foreign Core denial drift")
            else:
                raise I5RealQuestionError("foreign Core accessed owner's pending task")
        print("I5_REAL_QUESTION_FOREIGN_CORE_FIVE_METHODS=DENIED")

        PHASE = "owner-question-pending-after-foreign-denials"
        still = await owner.call("task.get", {"workdir": "Fixture", "task_id": task_id})
        require(
            still.get("status") == "waiting_for_question"
            and isinstance(still.get("pending_request"), dict),
            "question not still pending immediately before cancellation",
        )
        print("I5_REAL_QUESTION_WAITING_BEFORE_CANCEL=PASS")

        outcome = await asyncio.wait_for(
            owner.call("task.cancel", {"workdir": "Fixture", "task_id": task_id}),
            timeout=15,
        )
        require(outcome.get("status") == "cancelled", "owning Core cancel not confirmed")
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            now = await owner.call("task.get", {"workdir": "Fixture", "task_id": task_id})
            if now.get("status") == "cancelled":
                require(
                    now.get("pending_request_id") is None and now.get("pending_request") is None,
                    "cancelled question still has a pending request",
                )
                cancelled = True
                break
            require(now.get("status") not in TERMINAL, "question cancelled to wrong terminal")
            await asyncio.sleep(0.2)
        require(cancelled, "real pending question terminal cancellation not observed")
        print("I5_REAL_QUESTION_OWNER_TERMINAL_CANCELLED=PASS")

        try:
            await owner.call("task.question.answer", response)
        except AgentBridgeRemoteError as exc:
            require(exc.code == "REQUEST_STALE", "late question answer was not stale")
        else:
            raise I5RealQuestionError("late owner answered a cancelled question")
        print("I5_REAL_QUESTION_LATE_OWNER_ANSWER=REQUEST_STALE")

        final_events = await owner.call(
            "task.events",
            {"workdir": "Fixture", "task_id": task_id, "after_event_id": 0, "limit": 100},
        )
        final_kinds = safe_events(final_events.get("events"))
        require(
            "question.requested" in final_kinds
            and "task.cancelled" in final_kinds
            and "question.answered" not in final_kinds
            and "task.completed" not in final_kinds,
            "real cancelled pending question event audit invalid",
        )
        print("I5_REAL_QUESTION_NO_LATE_COMPLETION=PASS")
        return task_id
    finally:
        if task_id is not None and not cancelled:
            try:
                reply = await asyncio.wait_for(
                    owner.call("task.cancel", {"workdir": "Fixture", "task_id": task_id}),
                    timeout=12,
                )
                if reply.get("status") == "cancelled":
                    final_state = await asyncio.wait_for(
                        owner.call("task.get", {"workdir": "Fixture", "task_id": task_id}),
                        timeout=6,
                    )
                    if (
                        final_state.get("status") == "cancelled"
                        and final_state.get("pending_request_id") is None
                        and final_state.get("pending_request") is None
                    ):
                        print("I5_REAL_QUESTION_FINALLY_OWNER_CANCEL=VERIFIED")
                    else:
                        print("I5_REAL_QUESTION_FINALLY_OWNER_CANCEL=REQUESTED")
                else:
                    print("I5_REAL_QUESTION_FINALLY_OWNER_CANCEL=REQUESTED")
            except Exception:
                print("I5_REAL_QUESTION_FINALLY_OWNER_CANCEL=UNVERIFIED")


def prove_durable_after_stop(task_id: str) -> None:
    import subprocess

    from serverfs_mcp.windows_v25 import storage

    worker = storage.root(BRIDGE) / "envs/agent-bridge/Scripts/python.exe"
    proof_script = Path(__file__).with_name("v0250-i5-real-question-proof-worker.py")
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
    marker = b"I5_REAL_QUESTION_PRIVATE_STALE_SQLITE=PASS"
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

    require(sys.argv[1:] == ["--run-once"], "real Provider question one-shot required")
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
    smoke = helper("v0250-i5-provider-installed-smoke.py", "i5_real_question_native_lifecycle")
    real = helper("v0250-i5-real-claude-task-ownership-smoke.py", "i5_real_question_http_mapping")
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
        PHASE = "one-real-SDK-AskUserQuestion-await-and-cancel"
        result_id = asyncio.run(asyncio.wait_for(run_question(bindings, keys, real), timeout=140))
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
    print("I5_REAL_QUESTION_THREE_FIXTURES_STOP=" + ("VERIFIED" if cleanup else "UNVERIFIED"))
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
    print("I5_REAL_QUESTION_PROTECTED_PRODUCTION=UNCHANGED")
    if failed is not None:
        PHASE = failed_phase
        raise failed
    require(result_id is not None, "real Provider question task ID unavailable")
    PHASE = "independent-private-Worker-stale-request-proof"
    prove_durable_after_stop(result_id)
    print("I5_REAL_PROVIDER_PENDING_QUESTION_CANCEL_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no raw Provider or private paths
        print(
            "I5_REAL_QUESTION=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

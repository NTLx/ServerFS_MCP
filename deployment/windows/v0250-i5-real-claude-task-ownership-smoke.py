"""One-shot I5 installed Core HTTP -> signed shared Bridge -> REAL Claude task gate.

Only the three disposable I5 Scheduler components may run. The two Cores
share i5-shared's explicitly writable *private* Fixture and individual HMAC
grants. Submit exactly one tiny no-tools real Claude task through Core A's
authenticated MCP endpoint, verify B's cross-Core task access/cancel denial,
and require exact final output plus zero tool/command/file-change events.

The real Claude Code SDK may use the current user's native authentication
and may create ordinary user session history. Do not print model text, SDK
errors, credentials, task IDs, paths, native session IDs, or event payloads.

A timeout, unexpected approval/question, tool use, failed task or ambiguous
cleanup fails closed. No automatic retries, Provider approval, Git, Repo Ops,
production tasks, arbitrary files or public Connector/DNS are used.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

BRIDGE = "i5-shared"
CORES = {"i5-core-a": 18873, "i5-core-b": 18874}
SENTINEL = "I5_REAL_AGENT_OK"
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
UNSAFE_EVENTS = frozenset(
    {"tool.started", "command.started", "file_change.started", "file.changed", "command.completed"}
)
PHASE = "entry"


class I5RealTaskError(RuntimeError):
    """Fixed-stage failure; never expose raw model or provider output."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5RealTaskError(reason)


def helper(filename: str, name: str):
    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, source)
    require(spec is not None and spec.loader is not None, "I5 native helper missing")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def result_mapping(response) -> dict:
    require(
        getattr(response, "is_error", getattr(response, "isError", False)) is False,
        "I5 public MCP Agent submission was rejected",
    )
    body = getattr(response, "structured_content", None)
    if body is None:
        body = getattr(response, "structuredContent", None)
    if not isinstance(body, dict):
        for part in getattr(response, "content", []):
            if getattr(part, "type", "") == "text":
                body = json.loads(part.text)
                break
    require(isinstance(body, dict), "I5 public MCP Agent result was not a mapping")
    body = body.get("result", body)
    require(isinstance(body, dict), "I5 public MCP Agent result body was invalid")
    return body


def assert_no_unsafe_event(events: object) -> int:
    require(isinstance(events, list), "I5 task event payload invalid")
    for event in events:
        require(isinstance(event, dict), "I5 task event record malformed")
        kind = event.get("event_type")
        require(isinstance(kind, str), "I5 task event type missing")
        require(
            kind not in UNSAFE_EVENTS
            and not kind.startswith(("tool.", "command.", "file_change.")),
            "I5 real Claude task used a prohibited tool or command",
        )
    return len(events)


async def _run_real_task(keys: dict[str, str], bindings: dict) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from serverfs_mcp.agent_client import AgentBridgeClientError, AgentBridgeRemoteError

    _, client_a = bindings["i5-core-a"].build_wiring()
    _, client_b = bindings["i5-core-b"].build_wiring()
    task_id: str | None = None
    task_terminal = False
    try:
        # This is an actual public MCP submission, not a direct fake provider
        # or one-off standalone SDK client. Installed Core A signs for Bridge.
        url = f"http://127.0.0.1:{CORES['i5-core-a']}/mcp"
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + keys["i5-core-a"]},
                trust_env=False,
            ) as http,
            streamable_http_client(url, http_client=http) as (read, write),
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                catalog = {tool.name for tool in (await session.list_tools()).tools}
                require(
                    {"submit_agent_task", "get_agent_task", "read_agent_task_events"} <= catalog,
                    "I5 public Core A task tools are not registered",
                )
                reply = await session.call_tool(
                    "submit_agent_task",
                    {
                        "runtime": "claude",
                        "workdir": "Fixture",
                        "path": "",
                        "profile": "workspace-write",
                        "prompt": (
                            f"Respond with exactly {SENTINEL}. "
                            "Do not run commands, use tools, access files, "
                            "browse, install, edit, call MCP servers or request permissions."
                        ),
                    },
                )
                result = result_mapping(reply)
                task_id = result.get("task_id")
                require(
                    isinstance(task_id, str) and 4 <= len(task_id) <= 128,
                    "I5 MCP task submission returned no bounded task ID",
                )
                print("I5_REAL_TASK_SUBMITTED_VIA_CORE_A_HTTP=PASS")

        # The two Core identities both have authorized Fixture/Claude scope,
        # but ONLY task owner A may read or cancel the A-created task.
        for method in ("task.get", "task.events", "task.result.read", "task.cancel"):
            params = {"task_id": task_id}
            if method == "task.events":
                params["after_event_id"] = 0
                params["limit"] = 25
            if method == "task.result.read":
                params["offset_bytes"] = 0
                params["max_bytes"] = 1024
            # An explicitly scoped signed RPC exposes the authoritative Bridge
            # denial (CORE_TASK_NOT_FOUND), never the foreign task contents.
            try:
                await client_b.call(method, {**params, "workdir": "Fixture"})
            except AgentBridgeRemoteError as exc:
                require(
                    exc.code == "CORE_TASK_NOT_FOUND",
                    "I5 B foreign task denial used an unexpected status",
                )
            else:
                raise I5RealTaskError("I5 Core B accessed Core A's task")
        print("I5_FOREIGN_CORE_SCOPED_TASK_METHODS=DENIED")

        # Public MCP task APIs have no workdir argument. The Core client
        # probes its authorized workdirs and intentionally collapses all
        # foreign-task errors into one non-enumerating local error. This is
        # NOT an AgentBridgeRemoteError; the initial test was incorrect.
        for method in ("task.get", "task.cancel"):
            try:
                await client_b.call(method, {"task_id": task_id})
            except AgentBridgeClientError as exc:
                require(
                    type(exc) is AgentBridgeClientError
                    and str(exc) == "Bridge v2 Core capability does not authorize this request",
                    "I5 B public task client denial differs",
                )
            else:
                raise I5RealTaskError("I5 B public task API exposed A's task")
        print("I5_FOREIGN_CORE_PUBLIC_TASK_LOOKUP=DENIED")

        deadline = time.monotonic() + 100.0
        state = None
        while time.monotonic() < deadline:
            state = await client_a.call("task.get", {"task_id": task_id})
            status = state.get("status")
            require(isinstance(status, str), "I5 real task state absent")
            if status in TERMINAL:
                task_terminal = True
                break
            require(
                status not in {"waiting_for_approval", "waiting_for_question"},
                "I5 unexpected Provider approval or question: deny all",
            )
            await asyncio.sleep(0.8)
        require(task_terminal, "I5 real Claude task exceeded bounded deadline")
        require(
            state.get("status") == "succeeded"
            and isinstance(state.get("final_response"), str)
            and state["final_response"].strip() == SENTINEL,
            "I5 real Claude result not successful or not the exact sentinel",
        )
        print("I5_REAL_CLAUDE_TASK_COMPLETED=PASS")

        cursor, seen = 0, 0
        for _ in range(20):
            row = await client_a.call(
                "task.events",
                {"task_id": task_id, "after_event_id": cursor, "limit": 100},
            )
            events = row.get("events")
            seen += assert_no_unsafe_event(events)
            after = row.get("next_after_event_id")
            require(
                type(after) is int and after >= cursor,
                "I5 task event cursor malformed",
            )
            if not events:
                break
            require(after > cursor, "I5 task events did not advance")
            cursor = after
        else:
            raise I5RealTaskError("I5 event audit exceeded bounded pages")
        require(seen >= 1, "I5 real task produced no auditable events")
        print("I5_REAL_PROVIDER_TOOL_EVENTS=ZERO")
        print("I5_OWNING_CORE_TASK_GET_AND_EVENTS=PASS")
    finally:
        # A test task may still be active even if the HTTP call / cross-Core
        # proof / Provider turn fails. Attempt only its owner's scoped cancel.
        # The Scheduler-scoped outer finally will stop the entire I5 Bridge Job.
        if task_id is not None and not task_terminal:
            try:
                await asyncio.wait_for(
                    client_a.call("task.cancel", {"task_id": task_id}), timeout=12
                )
                print("I5_REAL_PROVIDER_PENDING_TASK=CANCEL_REQUESTED")
            except Exception:
                print("I5_REAL_PROVIDER_PENDING_TASK=CANCEL_UNVERIFIED")


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity

    PHASE = "installed-non-elevated-interpreter"
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I5 installed non-elevated Python -I -B required",
    )
    expected = storage.root(BRIDGE) / "envs" / "bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I5 real task gate requires installed Bridge owner Python",
    )

    smoke = helper("v0250-i5-provider-installed-smoke.py", "i5_installed_no_model_helper")
    gate = smoke.load_gate()
    PHASE = "private-protected-scheduler-baseline"
    baseline = smoke.protected_snapshot(gate)
    smoke.candidates_disabled(gate)
    prod_core = storage.load("core")
    generation = _current_identity(prod_core)
    require(generation is not None, "I5 production Core generation cannot be proven")

    PHASE = "private-exact-fixture-and-grants"
    sid = current_user_sid()
    fixture = storage.root(BRIDGE) / "workdirs" / "fixture"
    storage.check_private(fixture, directory=True, sid=sid)
    require(
        fixture.is_dir() and not fixture.is_symlink(),
        "I5 bounded private writable Fixture unavailable",
    )
    bindings: dict = {}
    keys: dict[str, str] = {}
    for core, port in CORES.items():
        manifest = storage.load("core", core)
        require(
            manifest.instance_id == core and manifest.settings["port"] == port,
            "I5 installed Core identity/port is not its own",
        )
        workdirs, policy = load_native_config(Path(str(manifest.settings["config"])))
        require(
            policy.bridge_required
            and len(workdirs) == 1
            and workdirs[0].alias == "Fixture"
            and workdirs[0].root.resolve(strict=True) == fixture.resolve(strict=True)
            and workdirs[0].agent_runtimes == frozenset({"claude"})
            and not workdirs[0].read_only,
            "I5 Core policy widened beyond disposable Fixture",
        )
        binding = load_native_core_bridge_binding(manifest, workdirs)
        require(
            binding.bridge_mode == "shared"
            and binding.bridge_id == BRIDGE
            and binding.instance_id == core,
            "I5 installed Core signed scope differs",
        )
        bindings[core] = binding
        auth = storage.home("core", core) / "http.env"
        storage.check_private(auth, directory=False, sid=sid)
        token = _read_env_file(auth).get("SERVERFS_HTTP_API_KEY")
        require(
            isinstance(token, str) and len(token) >= 40,
            "I5 private HTTP auth key missing",
        )
        keys[core] = token
    require(len(set(keys.values())) == 2, "I5 two HTTP auth secrets are not independent")

    started: list[tuple[str, str]] = []
    failure: BaseException | None = None
    failed_phase = ""
    cleanup = False
    try:
        PHASE = "scoped-bridge-start"
        started.append(("bridge", BRIDGE))
        require(
            smoke.cli("bridge", BRIDGE, "start").get("readiness") == "owner_alive",
            "I5 installed Bridge did not reach owner readiness",
        )
        for core in CORES:
            PHASE = "scoped-core-start-" + core
            started.append(("core", core))
            require(
                smoke.cli("core", core, "start").get("readiness") == "verified",
                "I5 installed Core not HTTP-ready",
            )
        PHASE = "one-real-claude-task-with-cross-core-denials"
        asyncio.run(asyncio.wait_for(_run_real_task(keys, bindings), timeout=140))
    except BaseException as exc:
        failure, failed_phase = exc, PHASE
    finally:
        PHASE = "scoped-I5-reverse-stop"
        ok = True
        for component, instance in reversed(started):
            try:
                row = smoke.cli(component, instance, "stop")
                ok = ok and (
                    row.get("state") == "Disabled"
                    and row.get("enabled") is False
                    and row.get("running") is False
                )
            except Exception:
                ok = False
        try:
            smoke.candidates_disabled(gate)
        except Exception:
            ok = False
        cleanup = ok
    print("I5_REAL_PROVIDER_FIXTURE_STOP=" + ("VERIFIED" if cleanup else "UNVERIFIED"))
    require(cleanup, "I5 real Provider fixture did not stop cleanly")
    PHASE = "protected-Scheduler-and-production-generation-postflight"
    require(
        smoke.protected_snapshot(gate) == baseline,
        "I5 real Provider task altered protected Scheduler baseline",
    )
    after = _current_identity(prod_core)
    require(
        after is not None
        and after.pid == generation.pid
        and after.creation_time == generation.creation_time,
        "I5 real Provider task changed production Core process generation",
    )
    print("I5_REAL_PROVIDER_PRODUCTION_AND_I1_I4=UNCHANGED")
    if failure is not None:
        PHASE = failed_phase
        raise failure
    print("I5_REAL_CORE_BRIDGE_CLAUDE_OWNERSHIP=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - never reveal provider contents
        print(
            "I5_REAL_PROVIDER_SMOKE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

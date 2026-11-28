"""Windows I11: one-shot REAL Claude tasks across TWO independent owned Bridges.

Starts only the four preinstalled Disabled I11 test services. Concurrently
submits one tiny no-tools text-only Claude task to EACH authenticated Core
HTTP MCP endpoint; independently checks two distinct response sentinels,
event audit and symmetric cross-Bridge task denial. SDK may consume the
current user's Claude quota/session; do not publish model text, task IDs,
private roots, prompts, errors, credentials or events.

No repository/file/command/MCP tool usage by Provider is authorized, no
interactive approvals/questions, no Cloudflare/Connector, no production.
All four test services are stopped in finally. This script is single-use;
never automatically rerun if it fails.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import os
import sys
import time
from pathlib import Path

IDS = ("i11-a", "i11-b")
PORTS = {"i11-a": 18881, "i11-b": 18882}
SENTINELS = {"i11-a": "I11_OWNED_REAL_A_OK", "i11-b": "I11_OWNED_REAL_B_OK"}
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
PHASE = "entry"


class I11ProviderRefusal(RuntimeError):
    """Non-secret deterministic refusal."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I11ProviderRefusal(reason)


def helper(filename: str, label: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, path)
    require(spec is not None and spec.loader is not None, "fixed helper missing")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


async def submit_one(instance: str, key: str, tasks: dict[str, str], previous) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    require(instance in IDS, "I11 submission identity not authorized")
    endpoint = f"http://127.0.0.1:{PORTS[instance]}/mcp"
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + key},
            trust_env=False,
            timeout=30,
            follow_redirects=False,
        ) as http,
        streamable_http_client(endpoint, http_client=http) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        require(
            {"submit_agent_task", "get_agent_task", "read_agent_task_events"} <= names,
            "I11 authenticated task HTTP API missing",
        )
        result = await session.call_tool(
            "submit_agent_task",
            {
                "runtime": "claude",
                "workdir": "Fixture",
                "path": "",
                "profile": "workspace-write",
                "prompt": (
                    f"Respond exactly {SENTINELS[instance]}. Output nothing else. "
                    "This is a text-only isolation test. Never execute commands, "
                    "access files, call tools or MCP servers, browse, modify "
                    "repositories, or request permissions/questions."
                ),
            },
        )
        task = previous.result_mapping(result).get("task_id")
        require(
            isinstance(task, str) and 4 <= len(task) <= 128,
            "I11 task submission did not return a bounded task identity",
        )
        tasks[instance] = task


async def foreign_deny(clients: dict, tasks: dict) -> None:
    from serverfs_mcp.agent_client import AgentBridgeRemoteError

    require(
        set(tasks) == set(IDS) and tasks[IDS[0]] != tasks[IDS[1]],
        "I11 distinct task identities missing",
    )
    for owner, stranger in ((IDS[0], IDS[1]), (IDS[1], IDS[0])):
        for method in ("task.get", "task.events", "task.result.read", "task.cancel"):
            params = {"workdir": "Fixture", "task_id": tasks[owner]}
            if method == "task.events":
                params.update(after_event_id=0, limit=25)
            elif method == "task.result.read":
                params.update(offset_bytes=0, max_bytes=256)
            try:
                await clients[stranger].call(method, params)
            except AgentBridgeRemoteError as exc:
                require(
                    exc.code == "CORE_TASK_NOT_FOUND",
                    "I11 foreign owned Bridge returned unexpected task denial",
                )
            else:
                raise I11ProviderRefusal("I11 foreign task method was accepted")
    print("I11_SYMMETRIC_FOREIGN_BRIDGE_FOUR_TASK_METHODS=DENIED")


async def await_both(clients: dict, tasks: dict, terminal: dict, previous) -> None:
    deadline = time.monotonic() + 115
    finals: dict[str, dict] = {}
    while time.monotonic() < deadline:
        replies = await asyncio.gather(
            *(
                clients[instance].call(
                    "task.get", {"workdir": "Fixture", "task_id": tasks[instance]}
                )
                for instance in IDS
            )
        )
        for instance, reply in zip(IDS, replies, strict=True):
            require(isinstance(reply, dict), "I11 private task status malformed")
            status = reply.get("status")
            require(isinstance(status, str), "I11 private task status absent")
            require(
                status not in {"waiting_for_approval", "waiting_for_question"},
                "I11 unexpected Claude interactive request",
            )
            if status in TERMINAL:
                terminal[instance] = True
                finals[instance] = reply
        if len(finals) == len(IDS):
            break
        await asyncio.sleep(0.8)
    require(len(finals) == len(IDS), "I11 real Provider pair deadline exceeded")
    for instance in IDS:
        result = finals[instance]
        require(
            result.get("status") == "succeeded"
            and isinstance(result.get("final_response"), str)
            and result["final_response"].strip() == SENTINELS[instance],
            "I11 Claude response not equal to exact instance sentinel",
        )
    print("I11_TWO_REAL_CLAUDE_OWNED_TASKS_EXACT=PASS")

    for instance in IDS:
        cursor = 0
        event_total = 0
        for _ in range(20):
            row = await clients[instance].call(
                "task.events",
                {
                    "workdir": "Fixture",
                    "task_id": tasks[instance],
                    "after_event_id": cursor,
                    "limit": 100,
                },
            )
            require(isinstance(row, dict), "I11 event record not a mapping")
            events = row.get("events")
            event_total += previous.assert_no_unsafe_event(events)
            next_cursor = row.get("next_after_event_id")
            require(type(next_cursor) is int and next_cursor >= cursor, "I11 event cursor drift")
            if not events:
                break
            require(next_cursor > cursor, "I11 event page does not advance")
            cursor = next_cursor
        else:
            raise I11ProviderRefusal("I11 event pagination exceeded bounded limit")
        require(event_total > 0, "I11 no durable events for real Claude task")
    print("I11_TWO_OWNED_BRIDGE_PROVIDER_TOOL_EVENTS=ZERO")


async def real_pair(keys: dict, bindings: dict) -> None:
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_peer_job import make_bridge_peer_validator

    previous = helper(
        "v0250-i5-real-claude-task-ownership-smoke.py", "i11_proven_real_provider_helpers"
    )
    require(set(keys) == set(bindings) == set(IDS), "I11 independent Core scope differs")
    clients = {}
    for instance in IDS:
        binding = bindings[instance]
        require(
            binding.bridge_mode == "owned" and binding.bridge_id == binding.instance_id == instance,
            "I11 real Provider Core is not bound to owned Bridge",
        )
        validator = make_bridge_peer_validator(bridge_id=instance, user_sid=storage._sid())
        _config, clients[instance] = binding.build_wiring(peer_validator=validator)

    tasks: dict[str, str] = {}
    terminal: dict[str, bool] = {}
    try:
        results = await asyncio.gather(
            *(submit_one(instance, keys[instance], tasks, previous) for instance in IDS),
            return_exceptions=True,
        )
        require(
            all(item is None for item in results), "I11 one of two HTTP task submissions rejected"
        )
        print("I11_TWO_CONCURRENT_REAL_HTTP_MCP_TASK_SUBMISSIONS=PASS")
        await foreign_deny(clients, tasks)
        await await_both(clients, tasks, terminal, previous)
    finally:
        # An earlier failure must not leak whichever Provider task did start.
        for instance, task_id in tasks.items():
            if terminal.get(instance):
                continue
            try:
                await asyncio.wait_for(
                    clients[instance].call(
                        "task.cancel", {"workdir": "Fixture", "task_id": task_id}
                    ),
                    timeout=14,
                )
                print("I11_" + instance.upper().replace("-", "_") + "_PENDING_CANCEL=REQUESTED")
            except BaseException:
                print("I11_" + instance.upper().replace("-", "_") + "_PENDING_CANCEL=UNVERIFIED")


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(sys.argv[1:] == ["--run-once"], "I11 real Claude gate requires one-use action")
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "nonadmin native Windows current SID -I -B required",
    )
    expected_python = storage.root(IDS[0]) / "envs/bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected_python.resolve(strict=True),
        "installed private I11 Bridge A Owner interpreter required",
    )
    i11 = helper("v0250-i11-two-owned-provider-stage.py", "i11_real_provider_gate")
    runtime = helper("v0250-i11-two-owned-provider-no-model-smoke.py", "i11_scoped_runtime")
    i4 = helper("v0250-i4-independent-live-smoke.py", "i11_real_protected_snapshot")
    gate = helper("v0250-i4-independent-preflight.py", "i11_real_protected_gate")
    PHASE = "exact-four-disabled-I11-owners-and-protected-preflight"
    i11.postflight()
    protected = i4.protected_baseline(gate)
    prod = storage.load("core")
    generation = _current_identity(prod)
    require(generation is not None, "I11 original production owner not running")
    keys = {}
    bindings = {}
    for instance, port in PORTS.items():
        manifest = storage.load("core", instance)
        workdirs, policy = load_native_config(Path(str(manifest.settings["config"])))
        fixture = storage.root(instance) / "workdirs/fixture"
        storage.check_private(fixture, directory=True, sid=storage._sid())
        require(
            manifest.settings["port"] == port
            and policy.bridge_required
            and len(workdirs) == 1
            and workdirs[0].alias == "Fixture"
            and workdirs[0].root.resolve(strict=True) == fixture.resolve(strict=True)
            and not workdirs[0].read_only
            and workdirs[0].agent_runtimes == frozenset({"claude"})
            and not workdirs[0].policy.repo_ops_enabled,
            "I11 disposable Provider policy or port drift",
        )
        bindings[instance] = load_native_core_bridge_binding(manifest, workdirs)
        auth = storage.home("core", instance) / "http.env"
        storage.check_private(auth, directory=False, sid=storage._sid())
        token = _read_env_file(auth).get("SERVERFS_HTTP_API_KEY")
        require(isinstance(token, str) and len(token) >= 40, "I11 private HTTP Bearer absent")
        keys[instance] = token
    require(keys[IDS[0]] != keys[IDS[1]], "I11 distinct Core Bearers overlap")

    started = []
    failure: BaseException | None = None
    failed_phase = ""
    cleaned = True
    try:
        for instance in IDS:
            PHASE = "start-owned-Bridge-" + instance
            started.append(("bridge", instance))
            require(
                runtime.cli("bridge", instance, "start").get("readiness") == "owner_alive",
                "I11 owned Bridge start unverified",
            )
        for instance in IDS:
            PHASE = "start-owned-Core-" + instance
            started.append(("core", instance))
            require(
                runtime.cli("core", instance, "start").get("readiness") == "verified",
                "I11 owned Core start unverified",
            )
        PHASE = "two-real-Provider-tasks-and-symmetric-foreign-ownership"
        asyncio.run(asyncio.wait_for(real_pair(keys, bindings), timeout=165))
    except BaseException as exc:  # noqa: BLE001 - no raw Provider errors leave host
        failure, failed_phase = exc, PHASE
    finally:
        PHASE = "stop-only-four-I11-owned-Agent-fixtures"
        for component, instance in reversed(started):
            try:
                status = runtime.cli(component, instance, "status")
                if not runtime.is_disabled(status):
                    status = runtime.cli(component, instance, "stop")
                cleaned = runtime.is_disabled(status) and cleaned
            except BaseException:
                cleaned = False
    del keys
    print("I11_REAL_PROVIDER_FOUR_OWNERS_DISABLED=" + ("PASS" if cleaned else "UNVERIFIED"))
    require(cleaned, "I11 real task scoped cleanup not verified")
    PHASE = "native-independent-postflight-and-protected-owner-generation"
    i11.postflight()
    current = _current_identity(prod)
    require(
        runtime.same(generation, current) and protected == i4.protected_baseline(gate),
        "I11 real task changed protected production/foreign task baseline",
    )
    print("I11_REAL_PROVIDER_PRODUCTION_I4_I7=UNCHANGED")
    if failure is not None:
        PHASE = failed_phase
        raise failure
    print("I11_TWO_INDEPENDENT_REAL_CLAUDE_OWNERS_AND_TASK_ISOLATION=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - only stable sanitized phase and type
        print(
            "I11_TWO_REAL_CLAUDE_TASKS=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

"""I3 live native test: two independent HTTP Cores share one Bridge v2.

Run ONLY after I3 one-use read-only staging returned PASS and all three tasks
are registered Disabled. This test starts the isolated Bridge FIRST, then the
two independent Cores; authenticates both Core HMAC identities over one real
Windows Named Pipe and checks independent HTTP MCP read-only policy. It stops
the two Cores before the Bridge in a scoped finally block.

Never touches legacy/production tasks, provider runtimes or Repo Ops.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

BRIDGE = "i3-shared"
CORE_PORTS = {"i3-core-a": 18869, "i3-core-b": 18870}
TASKS = (("bridge", BRIDGE), *(("core", cid) for cid in CORE_PORTS))
PHASE = "entry"


class I3SmokeError(RuntimeError):
    """Sanitized isolated shared-Bridge native acceptance refusal."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I3SmokeError(message)


def mark(phase: str) -> None:
    global PHASE
    PHASE = phase


def gate():
    source = Path(__file__).with_name("v0250-i3-shared-preflight.py")
    spec = importlib.util.spec_from_file_location("i3_live_native_gate", source)
    require(spec is not None and spec.loader is not None, "I3 host gate unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scheduler_rows(preflight) -> list[dict]:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", preflight.SCRIPT],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=25,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0, "I3 native Scheduler inventory unavailable")
    try:
        rows = json.loads(result.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise I3SmokeError("I3 Scheduler snapshot malformed") from exc
    require(isinstance(rows, list), "I3 Scheduler snapshot is not a list")
    return rows


def production_snapshot(preflight) -> tuple[tuple[str, str, bool], ...]:
    rows = scheduler_rows(preflight)
    baseline = [r for r in rows if isinstance(r, dict) and r.get("name") in preflight.PRODUCTION]
    preflight.verify_task_inventory(baseline)
    return tuple(sorted((r["name"], r["state"], r["enabled"]) for r in baseline))


def three_task_states(preflight, expected: str = "Disabled") -> None:
    rows = scheduler_rows(preflight)
    for component, instance in TASKS:
        from serverfs_mcp.windows_v25 import storage

        manifest = storage.load(component, instance)
        name = manifest.task_name()
        require(name in preflight.TARGETS, "I3 task name differs from reserved identity")
        matches = [r for r in rows if r.get("name") == name]
        require(len(matches) == 1, "I3 Scheduler task identity is absent or ambiguous")
        require(
            matches[0].get("state") == expected and matches[0].get("enabled") is False,
            "I3 task is not Disabled",
        )


def cli(component: str, instance: str, action: str) -> dict:
    from serverfs_mcp.windows_v25 import storage

    require(component in ("core", "bridge"), "I3 component is not allowlisted")
    require((component, instance) in TASKS, "I3 action targets a foreign instance")
    require(action in ("start", "stop", "status"), "I3 action is not allowlisted")
    private_python = storage.root(instance) / "envs" / component / "Scripts" / "python.exe"
    storage.check_private(private_python, directory=False, sid=storage._sid())
    result = subprocess.run(
        [
            str(private_python),
            "-I",
            "-B",
            "-m",
            "serverfs_mcp.windows_v25.cli",
            component,
            action,
            "--instance",
            instance,
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=35,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        print("I3_CLI_" + component.upper() + "_" + action.upper() + "=FAILED", file=sys.stderr)
        raise I3SmokeError("scoped I3 lifecycle CLI failed")
    try:
        document = json.loads(result.stdout.decode("utf-8"))
        require(isinstance(document, dict), "I3 CLI response invalid")
        return document
    except (ValueError, UnicodeError) as exc:
        raise I3SmokeError("I3 CLI response malformed") from exc


def _mapping(response) -> dict:
    require(
        getattr(response, "is_error", getattr(response, "isError", False)) is False,
        "I3 read-only MCP call was rejected",
    )
    body = getattr(response, "structured_content", None)
    if body is None:
        body = getattr(response, "structuredContent", None)
    if isinstance(body, dict):
        value = body.get("result", body)
        if isinstance(value, dict):
            return value
    for entry in getattr(response, "content", []):
        if getattr(entry, "type", None) == "text":
            result = json.loads(entry.text)
            if isinstance(result, dict):
                return result.get("result", result)
    raise I3SmokeError("I3 read-only MCP tool result was not a mapping")


async def public_core_check(core_id: str, port: int, token: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"http://127.0.0.1:{port}/mcp"
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as http,
        streamable_http_client(url, http_client=http) as (reader, writer),
    ):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            require("list_workdirs" in names, "I3 public MCP file tool unavailable")
            require("list_agent_runtimes" in names, "I3 Agent listing tool unavailable")
            rows = _mapping(await session.call_tool("list_workdirs", {}))
            fixture = next(
                (
                    item
                    for item in rows.get("workdirs", [])
                    if isinstance(item, dict) and item.get("alias") == "Fixture"
                ),
                None,
            )
            require(
                isinstance(fixture, dict)
                and fixture.get("access") == "read-only"
                and fixture.get("agent_mode") == "review"
                and fixture.get("repo_ops") is False,
                "I3 Core public workdir policy differs",
            )
            runtimes = _mapping(await session.call_tool("list_agent_runtimes", {}))
            require(runtimes.get("runtimes") == [], "I3 runtime exposure is not disabled")
            attempt = await session.call_tool(
                "create_text_file",
                {
                    "workdir": "Fixture",
                    "path": "i3-denied-" + secrets.token_hex(8) + ".txt",
                    "content": "refuse-write\n",
                },
            )
            require(
                getattr(attempt, "is_error", getattr(attempt, "isError", False)) is True,
                "I3 Core unexpectedly permitted read-only fixture mutation",
            )
    print("I3_HTTP_" + core_id.upper().replace("-", "_") + "=READONLY_MCP_PASS")


async def shared_bridge_rpc(core_bindings: dict) -> None:
    from serverfs_mcp.agent_client import AgentBridgeRemoteError
    from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_peer_job import make_bridge_peer_validator

    sid = storage._sid()
    peer = make_bridge_peer_validator(bridge_id=BRIDGE, user_sid=sid)
    clients = {}
    for core_id, binding in core_bindings.items():
        _settings, client = binding.build_wiring(peer_validator=peer)
        clients[core_id] = client
        require(
            await client.call("bridge.ping", {"workdir": "Fixture"}) == {"status": "ok"},
            "I3 signed shared Bridge health was rejected",
        )
        require(
            await client.call("runtime.list", {}) == {"runtimes": []},
            "I3 read-only shared Bridge exposed runtimes",
        )
    print("I3_TWO_CORE_SIGNED_SHARED_PIPE=PASS")
    a, b = tuple(core_bindings)
    source = core_bindings[a]
    impostor = CoreGrantBridgeClient(
        Path(source.pipe_name),
        core_id=a,
        bridge_id=source.bridge_id,
        key_id=core_bindings[b].key_id,
        secret=core_bindings[b].secret,
        policy_revisions=dict(source.policy_revisions),
        server_pid_validator=peer,
    )
    try:
        await impostor.call("bridge.ping", {"workdir": "Fixture"})
    except AgentBridgeRemoteError as exc:
        require(exc.code == "CORE_NOT_AUTHORIZED", "I3 cross-Core spoof returned wrong error")
    else:
        raise I3SmokeError("I3 Bridge accepted cross-Core HMAC spoof")
    print("I3_CROSS_CORE_HMAC_SPOOF=DENIED")


def main() -> None:
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity

    mark("ordinary-user-and-private-interpreter")
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I3 native non-elevated Python -I -B required",
    )
    expected = storage.root(BRIDGE) / "envs" / "bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I3 smoke requires installed Bridge owner Python",
    )
    sid = current_user_sid()
    preflight = gate()
    mark("incumbent-and-core-preflight")
    baseline = production_snapshot(preflight)
    production_core = storage.load("core")
    before_identity = _current_identity(production_core)
    require(before_identity is not None, "production Core generation unknown")
    mark("three-disabled-task-preflight")
    three_task_states(preflight)
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan

    bridge = storage.load("bridge", BRIDGE)
    load_native_bridge_plan(bridge)
    bindings = {}
    auth = {}
    for core_id in CORE_PORTS:
        manifest = storage.load("core", core_id)
        workdirs, native_policy = load_native_config(Path(str(manifest.settings["config"])))
        require(native_policy.bridge_required, "I3 Core does not require a Bridge")
        bindings[core_id] = load_native_core_bridge_binding(manifest, workdirs)
        private_env = storage.home("core", core_id) / "http.env"
        storage.check_private(private_env, directory=False, sid=sid)
        credentials = _read_env_file(private_env)
        bearer = credentials.get("SERVERFS_HTTP_API_KEY")
        require(isinstance(bearer, str) and len(bearer) >= 32, "I3 Core HTTP key absent")
        auth[core_id] = bearer
    require(len(set(auth.values())) == 2, "I3 Core HTTP credentials are not independent")

    started: list[tuple[str, str]] = []
    failed: BaseException | None = None
    failed_phase = ""
    stopped = False
    try:
        mark("bridge-start")
        started.append(("bridge", BRIDGE))
        owner = cli("bridge", BRIDGE, "start")
        require(
            owner.get("running") is True and owner.get("readiness") == "owner_alive",
            "I3 Bridge owner not verified",
        )
        mark("signed-two-core-ipc")
        asyncio.run(asyncio.wait_for(shared_bridge_rpc(bindings), timeout=28))
        for core_id in CORE_PORTS:
            mark("core-start-" + core_id)
            started.append(("core", core_id))
            result = cli("core", core_id, "start")
            require(
                result.get("running") is True and result.get("readiness") == "verified",
                "I3 Core startup/readiness unverified",
            )
        for core_id, port in CORE_PORTS.items():
            mark("http-mcp-" + core_id)
            asyncio.run(
                asyncio.wait_for(public_core_check(core_id, port, auth[core_id]), timeout=45)
            )
    except BaseException as exc:
        failed_phase = PHASE
        failed = exc
    finally:
        mark("isolated-reverse-stop")
        stop_ok = True
        for component, instance in reversed(started):
            try:
                response = cli(component, instance, "stop")
                stop_ok = stop_ok and (
                    response.get("state") == "Disabled"
                    and response.get("running") is False
                    and response.get("enabled") is False
                    and cli(component, instance, "status").get("readiness") == "offline"
                )
            except Exception:
                stop_ok = False
        try:
            three_task_states(preflight)
        except Exception:
            stop_ok = False
        stopped = stop_ok
    print("I3_FIXTURE_STOP=" + ("VERIFIED" if stopped else "NOT_VERIFIED"))
    require(stopped, "I3 stopped states unverified; no arbitrary task kill")
    mark("production-postflight")
    require(production_snapshot(preflight) == baseline, "production Scheduler baseline changed")
    after = _current_identity(production_core)
    require(
        after is not None
        and after.pid == before_identity.pid
        and after.creation_time == before_identity.creation_time,
        "production Core process generation changed",
    )
    print("I3_PRODUCTION_TASKS_AND_CORE=UNCHANGED")
    if failed is not None:
        mark(failed_phase)
        raise failed
    print("I3_NATIVE_SHARED_BRIDGE=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - phase/type only
        print(
            "I3_NATIVE_SMOKE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

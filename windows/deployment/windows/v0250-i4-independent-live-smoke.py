"""Native I4 acceptance: two Task Scheduler-owned Core/Bridge pairs.

Requires the create-only I4 stage to have registered all four exact tasks as
Disabled, with independent private Owned Core/Bridge HMAC bindings. Starts
only those tasks, checks signed IPC, authenticated read-only HTTP MCP, Core A
restart, Bridge A stop while B survives, then scoped reverse cleanup.

No real Agent provider, Repo Ops, tunnel or production task modification.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

IDS = ("i4-a", "i4-b")
PORTS = {"i4-a": 18871, "i4-b": 18872}
TASKS = (("bridge", IDS[0]), ("bridge", IDS[1]), ("core", IDS[0]), ("core", IDS[1]))
PHASE = "entry"
WRONG_BEARER = "i4-wrong-bearer-" + "0" * 32
INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "i4-negative-auth", "version": "1"},
    },
}


class I4SmokeError(RuntimeError):
    """Do not disclose private credentials, paths or raw exception messages."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I4SmokeError(message)


def load_helper(filename: str, name: str):
    file = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, file)
    require(spec is not None and spec.loader is not None, "I4 native helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def same_generation(before, after) -> bool:
    return (
        before is not None
        and after is not None
        and before.pid == after.pid
        and before.creation_time == after.creation_time
    )


def new_generation(before, after) -> bool:
    return (
        before is not None
        and after is not None
        and (before.pid != after.pid or before.creation_time != after.creation_time)
    )


def protected_baseline(gate) -> tuple:
    rows = gate.scheduler_rows()
    gate.verify_task_rows(rows, require_candidates_absent=False)
    protected = set(gate.PRODUCTION) | set(gate.EXISTING_FIXTURES)
    return tuple(
        sorted(
            (row["name"], row["state"], row["enabled"]) for row in rows if row["name"] in protected
        )
    )


def four_disabled(gate) -> None:
    from serverfs_mcp.windows_v25 import storage

    rows = gate.scheduler_rows()
    gate.verify_task_rows(rows, require_candidates_absent=False)
    by_name = {row["name"]: row for row in rows}
    for component, instance in TASKS:
        manifest = storage.load(component, instance)
        require(manifest.task_name() in gate.TARGETS, "I4 Scheduler identity differs")
        row = by_name[manifest.task_name()]
        require(
            row["state"] == "Disabled" and row["enabled"] is False,
            "I4 scoped service is not Disabled",
        )


def cli(component: str, instance: str, action: str) -> dict:
    from serverfs_mcp.windows_v25 import storage

    require((component, instance) in TASKS, "I4 action targets a foreign owner")
    require(action in ("start", "stop", "status"), "I4 lifecycle action forbidden")
    python = storage.root(instance) / "envs" / component / "Scripts" / "python.exe"
    storage.check_private(python, directory=False, sid=storage._sid())
    result = subprocess.run(
        [
            str(python),
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
        check=False,
        timeout=40,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise I4SmokeError("I4 scoped native lifecycle CLI returned nonzero")
    try:
        row = json.loads(result.stdout.decode("utf-8"))
        require(isinstance(row, dict), "I4 CLI output must be JSON object")
        return row
    except (UnicodeError, ValueError, TypeError) as exc:
        raise I4SmokeError("I4 scoped CLI returned malformed JSON") from exc


async def signed_pair(bindings, sid: str) -> None:
    from serverfs_mcp.agent_client import AgentBridgeRemoteError
    from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient
    from serverfs_mcp.windows_v25.bridge_peer_job import make_bridge_peer_validator

    clients = {}
    for instance in IDS:
        binding = bindings[instance]
        require(
            binding.instance_id == binding.bridge_id == instance and binding.bridge_mode == "owned",
            "I4 Core binding is not owned",
        )
        validator = make_bridge_peer_validator(bridge_id=instance, user_sid=sid)
        _settings, client = binding.build_wiring(peer_validator=validator)
        clients[instance] = client
        require(
            await client.call("bridge.ping", {"workdir": "Fixture"}) == {"status": "ok"},
            "I4 owned-Bridge signed request refused",
        )
        require(
            await client.call("runtime.list", {}) == {"runtimes": []},
            "I4 read-only owned Bridge exposed an Agent runtime",
        )
    print("I4_TWO_OWNED_SIGNED_PIPES=PASS")

    a, b = bindings[IDS[0]], bindings[IDS[1]]
    validator_b = make_bridge_peer_validator(bridge_id=IDS[1], user_sid=sid)
    wrong = CoreGrantBridgeClient(
        Path(b.pipe_name),
        core_id=IDS[1],
        bridge_id=b.bridge_id,
        key_id=b.key_id,
        secret=a.secret,
        policy_revisions=dict(b.policy_revisions),
        server_pid_validator=validator_b,
    )
    try:
        await wrong.call("bridge.ping", {"workdir": "Fixture"})
    except AgentBridgeRemoteError as exc:
        require(exc.code == "CORE_NOT_AUTHORIZED", "I4 cross-Bridge rejection code differs")
    else:
        raise I4SmokeError("I4 Bridge accepted other instance's HMAC key")
    print("I4_CROSS_BRIDGE_HMAC=DENIED")


def wrong_bearer_request_headers(token: str) -> dict[str, str]:
    """Build the negative-auth headers; never reuse the accepted credential."""
    require(WRONG_BEARER != token, "I4 negative Bearer equals the live Core key")
    return {
        "Authorization": "Bearer " + WRONG_BEARER,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


async def wrong_bearer_is_refused(port: int, token: str) -> None:
    """Fail closed unless a wrong Bearer cannot initialize MCP on loopback."""
    import httpx2

    async with httpx2.AsyncClient(
        headers=wrong_bearer_request_headers(token),
        trust_env=False,
        timeout=10,
    ) as http:
        response = await http.post(
            f"http://127.0.0.1:{port}/mcp",
            json=INITIALIZE_REQUEST,
        )
        require(response.status_code == 401, "I4 wrong Bearer did not get HTTP 401")
        require(
            "mcp-session-id" not in {name.lower() for name in response.headers},
            "I4 wrong Bearer received an MCP session identity",
        )
        try:
            payload = json.loads(response.content) if response.content else None
        except (UnicodeError, ValueError):
            payload = None
        require(
            not (isinstance(payload, dict) and ("result" in payload or "error" in payload)),
            "I4 wrong Bearer received an MCP JSON-RPC response",
        )


async def http_file_policy(port: int, token: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"http://127.0.0.1:{port}/mcp"
    await wrong_bearer_is_refused(port, token)
    print("I4_WRONG_BEARER=DENIED_HTTP_401")
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as http,
        streamable_http_client(url, http_client=http) as (reader, writer),
    ):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            names = {item.name for item in (await session.list_tools()).tools}
            require("list_workdirs" in names, "I4 MCP file policy tool missing")
            response = await session.call_tool("list_workdirs", {})
            require(
                getattr(response, "is_error", getattr(response, "isError", True)) is False,
                "I4 list_workdirs is unexpectedly unavailable",
            )
            mapping = getattr(response, "structured_content", None)
            if mapping is None:
                mapping = getattr(response, "structuredContent", None)
            if not isinstance(mapping, dict):
                mapping = json.loads(response.content[0].text)
            result = mapping.get("result", mapping)
            records = result.get("workdirs")
            require(isinstance(records, list), "I4 HTTP MCP workdir response invalid")
            fixture = next((item for item in records if item.get("alias") == "Fixture"), None)
            require(
                isinstance(fixture, dict)
                and fixture.get("access") == "read-only"
                and fixture.get("agent_mode") == "review"
                and fixture.get("repo_ops") is False,
                "I4 HTTP Core workdir policy differs",
            )


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity

    PHASE = "native-and-private-interpreter"
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I4 native non-elevated Python -I -B required",
    )
    expected = storage.root(IDS[0]) / "envs" / "bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I4 smoke must use its installed private Bridge owner Python",
    )
    sid = current_user_sid()
    gate = load_helper("v0250-i4-independent-preflight.py", "i4_live_gate")
    previous = load_helper("v0250-i3-readonly-live-smoke.py", "i4_safe_http_helper")
    PHASE = "protected-preflight"
    baseline = protected_baseline(gate)
    four_disabled(gate)
    prod_core = storage.load("core")
    prod_generation = _current_identity(prod_core)
    require(prod_generation is not None, "I4 production Core generation unavailable")

    PHASE = "load-private-owned-bindings"
    bindings = {}
    tokens = {}
    for instance in IDS:
        manifest = storage.load("core", instance)
        workdirs, policy = load_native_config(Path(str(manifest.settings["config"])))
        require(policy.bridge_required, "I4 Core must require its owned Bridge")
        binding = load_native_core_bridge_binding(manifest, workdirs)
        require(
            binding.bridge_mode == "owned" and binding.instance_id == binding.bridge_id == instance,
            "I4 Core owned Bridge scope differs",
        )
        bindings[instance] = binding
        auth_file = storage.home("core", instance) / "http.env"
        storage.check_private(auth_file, directory=False, sid=sid)
        credentials = _read_env_file(auth_file)
        token = credentials.get("SERVERFS_HTTP_API_KEY")
        require(isinstance(token, str) and len(token) >= 32, "I4 private HTTP key invalid")
        tokens[instance] = token
    require(len(set(tokens.values())) == 2, "I4 HTTP Core credentials are not independent")

    started: list[tuple[str, str]] = []
    failure: BaseException | None = None
    failed_phase = ""
    cleanup_verified = False
    try:
        for instance in IDS:
            PHASE = "bridge-start-" + instance
            started.append(("bridge", instance))
            status = cli("bridge", instance, "start")
            require(
                status.get("running") is True and status.get("readiness") == "owner_alive",
                "I4 owned Bridge startup not verified",
            )
        PHASE = "signed-owned-pipes"
        asyncio.run(asyncio.wait_for(signed_pair(bindings, sid), timeout=35))
        for instance in IDS:
            PHASE = "core-start-" + instance
            started.append(("core", instance))
            status = cli("core", instance, "start")
            require(
                status.get("running") is True and status.get("readiness") == "verified",
                "I4 independent Core startup not verified",
            )
        for instance, port in PORTS.items():
            PHASE = "full-readonly-mcp-" + instance
            asyncio.run(
                asyncio.wait_for(
                    previous.public_core_check(instance, port, tokens[instance]), timeout=45
                )
            )
        print("I4_TWO_HTTP_CORES=READONLY_MCP_PASS")

        PHASE = "core-a-independent-restart"
        bridges_before = {
            identity: _current_identity(storage.load("bridge", identity)) for identity in IDS
        }
        other_core = storage.load("core", IDS[1])
        other_before = _current_identity(other_core)
        own_core = storage.load("core", IDS[0])
        own_before = _current_identity(own_core)
        require(
            all(bridges_before.values()) and other_before and own_before,
            "I4 process generation missing",
        )
        stopped = cli("core", IDS[0], "stop")
        require(stopped.get("state") == "Disabled", "I4 Core A stop unverified")
        restarted = cli("core", IDS[0], "start")
        require(restarted.get("running") is True, "I4 Core A restart unverified")
        require(
            new_generation(own_before, _current_identity(own_core)),
            "I4 Core A restart reused its previous process generation",
        )
        require(
            same_generation(other_before, _current_identity(other_core))
            and all(
                same_generation(
                    bridges_before[ident], _current_identity(storage.load("bridge", ident))
                )
                for ident in IDS
            ),
            "I4 foreign Core/Bridge restarted unexpectedly",
        )
        asyncio.run(asyncio.wait_for(http_file_policy(PORTS[IDS[0]], tokens[IDS[0]]), timeout=45))
        print("I4_CORE_A_RESTART_OTHER_OWNERS=UNCHANGED")

        PHASE = "bridge-a-independent-stop"
        cores_before = {ident: _current_identity(storage.load("core", ident)) for ident in IDS}
        other_bridge = storage.load("bridge", IDS[1])
        bridge_b_before = _current_identity(other_bridge)
        stopped = cli("bridge", IDS[0], "stop")
        require(stopped.get("state") == "Disabled", "I4 Bridge A stop unverified")
        require(
            all(
                same_generation(cores_before[ident], _current_identity(storage.load("core", ident)))
                for ident in IDS
            )
            and same_generation(bridge_b_before, _current_identity(other_bridge)),
            "I4 stopping Bridge A changed unrelated service generation",
        )
        for ident, port in PORTS.items():
            asyncio.run(asyncio.wait_for(http_file_policy(port, tokens[ident]), timeout=45))
        print("I4_BRIDGE_A_STOP_CORE_AND_BRIDGE_B=UNCHANGED")
    except BaseException as exc:
        failed_phase = PHASE
        failure = exc
    finally:
        PHASE = "reverse-scoped-stop"
        ok = True
        for component, instance in reversed(started):
            try:
                status = cli(component, instance, "stop")
                ok = ok and (
                    status.get("state") == "Disabled"
                    and status.get("running") is False
                    and status.get("enabled") is False
                )
            except Exception:
                ok = False
        try:
            four_disabled(gate)
        except Exception:
            ok = False
        cleanup_verified = ok
    print("I4_FIXTURE_STOP=" + ("VERIFIED" if cleanup_verified else "NOT_VERIFIED"))
    require(cleanup_verified, "I4 exact scoped fixture cleanup unverified")
    PHASE = "protected-postflight"
    require(protected_baseline(gate) == baseline, "I4 protected Scheduler state changed")
    require(
        same_generation(prod_generation, _current_identity(prod_core)), "I4 production Core changed"
    )
    print("I4_PRODUCTION_TASKS_AND_CORE=UNCHANGED")
    if failure is not None:
        PHASE = failed_phase
        raise failure
    print("I4_NATIVE_TWO_OWNED_PAIRS=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - sanitized phase/type only
        print(
            "I4_NATIVE_SMOKE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

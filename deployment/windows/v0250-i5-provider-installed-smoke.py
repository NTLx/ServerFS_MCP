"""One native I5 installed Provider Bridge/dual-Core *no-model* smoke.

Starts only three previously registered Disabled i5-* Scheduler tasks, verifies
signed v2 pipe grants and authenticated MCP HTTP/Claude runtime discovery, then
stops the three exact tasks in finally. No task.submit, model completion, Repo
Ops, file mutation or production task operation. Failure leaves fixed markers.
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

BRIDGE = "i5-shared"
CORES = {"i5-core-a": 18873, "i5-core-b": 18874}
TASKS = (("bridge", BRIDGE), *(("core", core) for core in CORES))
PHASE = "entry"


class I5NativeSmokeError(RuntimeError):
    """A nonsecret I5 native service acceptance refusal."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5NativeSmokeError(reason)


def load_gate():
    source = Path(__file__).with_name("v0250-i5-provider-shared-preflight.py")
    spec = importlib.util.spec_from_file_location("i5_live_known_preflight", source)
    require(spec is not None and spec.loader is not None, "I5 preflight code missing")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def protected_snapshot(gate) -> tuple:
    gate.protected_tasks()
    old = gate._i4_gate()
    rows = old.scheduler_rows()
    old.verify_task_rows(rows, require_candidates_absent=False)
    accepted = set(old.PRODUCTION) | set(old.EXISTING_FIXTURES) | set(old.TARGETS)
    return tuple(
        sorted(
            (row["name"], row["state"], row["enabled"]) for row in rows if row["name"] in accepted
        )
    )


def candidates_disabled(gate) -> None:
    gate.verify_candidates(gate.candidates(), installed=True)


def classify_cli_refusal(raw: bytes) -> str:
    """Classify only fixed native error phrases; never emit raw CLI stderr."""
    for stage in ("enable", "run", "ready"):
        if ("component start refused at " + stage).encode("ascii") in raw:
            return "START_" + stage.upper()
    if b"registered service task does not match owner contract" in raw:
        return "SCHEDULER_OWNER_XML"
    if b"Scheduler current-user state query is unavailable" in raw:
        return "SCHEDULER_QUERY"
    if b"registered service task" in raw:
        return "SCHEDULER_OWNER"
    if b"legacy Bridge writable ownership cannot be excluded" in raw:
        return "LEGACY_OWNERSHIP"
    if b"service refused (" in raw:
        return "NATIVE_PREFLIGHT_EXCEPTION"
    return "UNKNOWN"


def cli(component: str, instance: str, action: str) -> dict:
    from serverfs_mcp.windows_v25 import storage

    require((component, instance) in TASKS, "I5 lifecycle targets foreign task")
    require(action in ("start", "stop", "status"), "I5 lifecycle action disallowed")
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
        timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        print(
            "I5_SCOPED_CLI_REFUSAL="
            + component.upper()
            + "_"
            + action.upper()
            + "_"
            + classify_cli_refusal(result.stderr)
        )
        raise I5NativeSmokeError("I5 scoped lifecycle CLI refused")
    try:
        response = json.loads(result.stdout.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise I5NativeSmokeError("I5 scoped lifecycle output invalid") from exc
    require(isinstance(response, dict), "I5 CLI must return a state object")
    return response


async def signed_dual_core(grants: dict, sid: str) -> None:
    from serverfs_mcp.agent_client import AgentBridgeRemoteError
    from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient
    from serverfs_mcp.windows_v25.bridge_peer_job import make_bridge_peer_validator

    validator = make_bridge_peer_validator(bridge_id=BRIDGE, user_sid=sid)
    clients = {}
    for core_id, binding in grants.items():
        require(
            binding.bridge_id == BRIDGE
            and binding.instance_id == core_id
            and binding.bridge_mode == "shared",
            "I5 signed Core binding identity differs",
        )
        _settings, client = binding.build_wiring(peer_validator=validator)
        clients[core_id] = client
        require(
            await client.call("bridge.ping", {"workdir": "Fixture"}) == {"status": "ok"},
            "I5 Bridge health denied a signed Core",
        )
    print("I5_INSTALLED_TWO_CORE_SIGNED_PIPE=PASS")

    a, b = tuple(grants)
    forged = CoreGrantBridgeClient(
        Path(grants[b].pipe_name),
        core_id=b,
        bridge_id=BRIDGE,
        key_id=grants[b].key_id,
        secret=grants[a].secret,
        policy_revisions=dict(grants[b].policy_revisions),
        server_pid_validator=validator,
    )
    try:
        await forged.call("bridge.ping", {"workdir": "Fixture"})
    except AgentBridgeRemoteError as exc:
        require(exc.code == "CORE_NOT_AUTHORIZED", "I5 cross-Core HMAC denial differs")
    else:
        raise I5NativeSmokeError("I5 cross-Core HMAC forged request was accepted")
    print("I5_INSTALLED_CROSS_CORE_HMAC=DENIED")

    for client in clients.values():
        catalog = await client.call("runtime.list", {})
        runtimes = catalog.get("runtimes")
        require(
            isinstance(runtimes, list)
            and any(
                isinstance(row, dict)
                and row.get("name") == "claude"
                and row.get("available") is True
                for row in runtimes
            ),
            "I5 installed Claude runtime is not discoverable and available",
        )
    print("I5_INSTALLED_REAL_CLAUDE_RUNTIME=AVAILABLE_NO_MODEL")


async def authenticated_http(port: int, token: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"http://127.0.0.1:{port}/mcp"
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as http,
        streamable_http_client(url, http_client=http) as (read, write),
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            require(
                {"list_workdirs", "submit_agent_task", "get_agent_task"} <= tools,
                "I5 Agent-enabled MCP tools absent",
            )
            result = await session.call_tool("list_workdirs", {})
            require(
                getattr(result, "is_error", getattr(result, "isError", False)) is False,
                "I5 authenticated MCP workdir listing rejected",
            )
            mapping = getattr(result, "structured_content", None)
            if mapping is None:
                mapping = getattr(result, "structuredContent", None)
            if not isinstance(mapping, dict):
                mapping = json.loads(result.content[0].text)
            workdirs = mapping.get("result", mapping).get("workdirs")
            require(isinstance(workdirs, list), "I5 authenticated workdir list missing")
            fixture = next(
                (
                    item
                    for item in workdirs
                    if isinstance(item, dict) and item.get("alias") == "Fixture"
                ),
                None,
            )
            require(
                isinstance(fixture, dict)
                and fixture.get("access") == "read-write"
                and fixture.get("agent_mode") == "workspace-write"
                and fixture.get("repo_ops") is False,
                "I5 public Core Fixture Agent policy differs",
            )


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity

    PHASE = "private-bridge-owner-interpreter"
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I5 native non-elevated -I -B required",
    )
    expected = storage.root(BRIDGE) / "envs" / "bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I5 smoke requires its privately installed Bridge owner Python",
    )

    gate = load_gate()
    PHASE = "protected-and-candidate-disabled-preflight"
    baseline = protected_snapshot(gate)
    candidates_disabled(gate)
    production_core = storage.load("core")
    before = _current_identity(production_core)
    require(before is not None, "production Core generation unknown")
    sid = current_user_sid()

    # Distinguish CLI input/isolation rejection from Task Scheduler transition
    # without starting any owner or printing private source data.
    PHASE = "bridge-start-static-preflight"
    from serverfs_mcp.windows_v25.cli import _assert_instance_isolation, _preflight
    from serverfs_mcp.windows_v25.legacy_bridge_ownership import (
        assert_legacy_writable_roots_disjoint,
    )

    bridge_manifest = storage.load("bridge", BRIDGE)
    _preflight(bridge_manifest)
    print("I5_BRIDGE_START_PRIVATE_PREFLIGHT=PASS")
    _assert_instance_isolation(bridge_manifest)
    print("I5_BRIDGE_START_INSTALLED_PEER_ISOLATION=PASS")
    assert_legacy_writable_roots_disjoint(bridge_manifest)
    print("I5_BRIDGE_START_LEGACY_DISJOINTNESS=PASS")

    PHASE = "private-core-bindings-and-http-keys"
    bindings = {}
    keys = {}
    for core_id in CORES:
        manifest = storage.load("core", core_id)
        workdirs, settings = load_native_config(Path(str(manifest.settings["config"])))
        require(settings.bridge_required, "I5 Core not bound to Agent Bridge")
        bindings[core_id] = load_native_core_bridge_binding(manifest, workdirs)
        private = storage.home("core", core_id) / "http.env"
        storage.check_private(private, directory=False, sid=sid)
        data = _read_env_file(private)
        token = data.get("SERVERFS_HTTP_API_KEY")
        require(isinstance(token, str) and len(token) >= 40, "I5 Core HTTP key absent")
        keys[core_id] = token
    require(len(set(keys.values())) == 2, "I5 Core auth keys not independent")

    started = []
    failed = None
    failed_phase = ""
    clean = False
    try:
        PHASE = "isolated-bridge-start"
        started.append(("bridge", BRIDGE))
        owner = cli("bridge", BRIDGE, "start")
        require(
            owner.get("running") is True and owner.get("readiness") == "owner_alive",
            "I5 native owned Bridge not ready",
        )
        PHASE = "signed-claude-runtime-health"
        asyncio.run(asyncio.wait_for(signed_dual_core(bindings, sid), timeout=40))
        for core in CORES:
            PHASE = "isolated-core-start-" + core
            started.append(("core", core))
            status = cli("core", core, "start")
            require(
                status.get("running") is True and status.get("readiness") == "verified",
                "I5 Core startup not verified",
            )
        for core, port in CORES.items():
            PHASE = "authenticated-core-mcp-" + core
            asyncio.run(asyncio.wait_for(authenticated_http(port, keys[core]), timeout=45))
        print("I5_INSTALLED_TWO_AUTHENTICATED_HTTP_CORES=PASS")
    except BaseException as exc:
        failed = exc
        failed_phase = PHASE
    finally:
        PHASE = "reverse-isolated-stop"
        valid = True
        for component, instance in reversed(started):
            try:
                status = cli(component, instance, "stop")
                valid = valid and (
                    status.get("state") == "Disabled"
                    and status.get("enabled") is False
                    and status.get("running") is False
                )
            except Exception:
                valid = False
        try:
            candidates_disabled(gate)
        except Exception:
            valid = False
        clean = valid
    print("I5_INSTALLED_FIXTURE_STOP=" + ("VERIFIED" if clean else "UNVERIFIED"))
    require(clean, "I5 scoped disabled cleanup could not be verified")
    PHASE = "production-postflight"
    require(protected_snapshot(gate) == baseline, "I5 protected Scheduler baseline changed")
    after = _current_identity(production_core)
    require(
        after is not None
        and after.pid == before.pid
        and after.creation_time == before.creation_time,
        "I5 production Core generation changed",
    )
    print("I5_INSTALLED_PRODUCTION_AND_I1_I4=UNCHANGED")
    if failed is not None:
        PHASE = failed_phase
        raise failed
    print("I5_NATIVE_NO_MODEL_BRIDGE_AND_TWO_CORE=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - fixed phase/type only
        print(
            "I5_NATIVE_NO_MODEL_SMOKE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

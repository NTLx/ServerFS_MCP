"""Real public HTTPS/MCP dual I5 Core -> shared Bridge on native Windows.

Precondition (verified separately at Cloudflare control plane):
nonproduction Tunnel A ingress -> http://127.0.0.1:18873 with Host
i5-core-a.local, B -> :18874 Host i5-core-b.local, both with 404
fallback; original I7 routes saved and MUST be restored afterward.
Production hostname/Tunnel/DNS never modified.

Only I5 private Bridge/Core A/Core B and the two newly installed I5
Cloudflare Connector tasks can be started. Original I7 Connector owners and
I7 Core MUST remain Disabled and unavailable. Token and Core Bearer bytes
never leave Windows and are never printed. The two newly installed Connector
manifests must bind the actual I5 Core instance ID, Host and port.

Requires two independent remote authenticated MCP sessions, separate Bearers
and 401 cross-Core denial, bad Host/Origin rejection, Bridge-backed Claude
runtime discovery through real public HTTP without submitting any task,
and Connector A/Core A stop while Connector B/Core B/Bridge survive.
All scoped services are stopped in finally, even on public failure.

Temporary reuse of the two I7 nonprod Tunnel IDs requires their original
owners never start concurrently. This is one nonproduction acceptance probe,
NOT a persistent general-purpose Tunnel ownership/lease protocol.
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
ROUTES = (
    ("i5-core-a", "i5-edge-a", "workpc-v25a.ntlx.top", 18873),
    ("i5-core-b", "i5-edge-b", "workpc-v25b.ntlx.top", 18874),
)
PHASE = "entry"


class I5PublicBridgeRefusal(RuntimeError):
    """No raw auth/tokens/task IDs/provider output may escape."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5PublicBridgeRefusal(reason)


def helper(name: str, label: str):
    file = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, file)
    require(spec is not None and spec.loader is not None, "fixed helper missing")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def connector_cli(instance: str, connector: str, action: str) -> dict:
    from serverfs_mcp.windows_v25 import storage

    require(
        (instance, connector) in {(row[0], row[1]) for row in ROUTES}
        and action in ("start", "stop", "status"),
        "I5 public Connector lifecycle scope invalid",
    )
    python = storage.root(instance) / "envs/cloudflare/Scripts/python.exe"
    storage.check_private(python, directory=False, sid=storage._sid())
    command = [
        str(python),
        "-I",
        "-B",
        "-m",
        "serverfs_mcp.windows_v25.cli",
        "cloudflare",
        action,
        "--instance",
        instance,
        "--connector-id",
        connector,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=75,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0, "scoped I5 Cloudflare CLI refused")
    try:
        output = json.loads(result.stdout.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise I5PublicBridgeRefusal("I5 Connector CLI returned invalid JSON") from None
    require(isinstance(output, dict), "I5 Connector CLI returned invalid shape")
    return output


def original_i7_owners_disabled() -> None:
    """Check ONLY original I7 Tunnel task ownership while I5 peers are LIVE.

    The general I7 postflight checks all I5 test tasks are Disabled, so it
    must never be called inside a live I5 Core/Bridge session.
    """
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    scheduler = Scheduler()
    for connector in ("i7-edge-a", "i7-edge-b"):
        manifest = storage.load("cloudflare", "i7-public", connector)
        require(
            manifest.component == "cloudflare"
            and manifest.instance_id == "i7-public"
            and manifest.connector_id == connector,
            "original Tunnel Owner manifest identity differs",
        )
        state = scheduler.status(manifest)
        require(
            state.get("registered") is True
            and state.get("state") == "Disabled"
            and state.get("enabled") is False
            and state.get("running") is False
            and state.get("readiness") == "offline"
            and _current_identity(manifest) is None,
            "original I7 Tunnel Owner not Disabled/offline",
        )


def is_disabled(state: dict) -> bool:
    return (
        state.get("registered") is True
        and state.get("enabled") is False
        and state.get("state") == "Disabled"
        and state.get("running") is False
    )


def data(result) -> dict:
    require(
        not getattr(result, "is_error", getattr(result, "isError", True)), "public MCP tool refused"
    )
    payload = getattr(result, "structured_content", None)
    if payload is None:
        payload = getattr(result, "structuredContent", None)
    if not isinstance(payload, dict):
        content = getattr(result, "content", ())
        require(
            bool(content) and getattr(content[0], "type", "") == "text", "MCP result unavailable"
        )
        payload = json.loads(content[0].text)
    require(isinstance(payload, dict), "public MCP result shape invalid")
    payload = payload.get("result", payload)
    require(isinstance(payload, dict), "public MCP result body invalid")
    return payload


async def denials(host: str, key: str, foreign_key: str) -> None:
    import httpx2

    require(key != foreign_key, "public Cores reused Bearer")
    url = "https://" + host + "/mcp"
    body = {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "i5-bridge-public-nonprod", "version": "1"},
        },
    }
    head = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    async with httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=25.0) as client:
        missing = await client.post(url, headers=head, json=body)
        wrong = await client.post(
            url, headers={**head, "Authorization": "Bearer invalid-nonprod-probe"}, json=body
        )
        cross = await client.post(
            url, headers={**head, "Authorization": "Bearer " + foreign_key}, json=body
        )
        require(
            missing.status_code == wrong.status_code == cross.status_code == 401,
            "two distinct public Core Bearers did not reject missing/foreign token",
        )
        foreign_host = await client.post(
            url,
            headers={**head, "Authorization": "Bearer " + key, "Host": "untrusted.invalid"},
            json=body,
        )
        bad_origin = await client.post(
            url,
            headers={
                **head,
                "Authorization": "Bearer " + key,
                "Origin": "https://untrusted.invalid",
            },
            json=body,
        )
        require(
            foreign_host.status_code not in (200, 201, 202)
            and bad_origin.status_code not in (200, 201, 202),
            "foreign Host/Origin was authorized",
        )
    print("I5_PUBLIC_" + host.split(".")[0].upper().replace("-", "_") + "_DENIALS=PASS")


async def public_mcp(host: str, bearer: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = "https://" + host + "/mcp"
    async with (
        httpx2.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=30.0,
            headers={"Authorization": "Bearer " + bearer},
        ) as http,
        streamable_http_client(url, http_client=http) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        catalog = {tool.name for tool in (await session.list_tools()).tools}
        require(
            {"list_workdirs", "list_agent_runtimes", "submit_agent_task", "get_agent_task"}
            <= catalog,
            "public Bridge-backed MCP catalog unavailable",
        )
        require(
            "run_repo_action" not in catalog,
            "disposable Provider Fixture public MCP exposed repo operations",
        )
        listing = data(await session.call_tool("list_workdirs", {}))
        fixture = next(
            (
                item
                for item in listing.get("workdirs", ())
                if isinstance(item, dict) and item.get("alias") == "Fixture"
            ),
            None,
        )
        require(
            fixture is not None
            and fixture.get("access") == "read-write"
            and fixture.get("agent_mode") == "workspace-write"
            and fixture.get("repo_ops") is False,
            "public Fixture policy differs from I5 provider test-only scope",
        )
        runtimes = data(await session.call_tool("list_agent_runtimes", {}))
        require(
            isinstance(runtimes.get("runtimes"), list)
            and any(
                isinstance(row, dict)
                and row.get("name") == "claude"
                and row.get("available") is True
                for row in runtimes["runtimes"]
            ),
            "real shared Bridge Claude runtime not discoverable through public MCP",
        )
    print("I5_PUBLIC_" + host.split(".")[0].upper().replace("-", "_") + "_BRIDGE_MCP=PASS")


async def public_both(keys: dict[str, str]) -> None:
    await asyncio.gather(
        public_mcp(ROUTES[0][2], keys[ROUTES[0][0]]),
        public_mcp(ROUTES[1][2], keys[ROUTES[1][0]]),
    )
    print("I5_PUBLIC_TWO_INDEPENDENT_BRIDGE_MCP_SESSIONS=PASS")


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(sys.argv[1:] == ["--run-fixed-once"], "corrected one-shot public test required")
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "native unprivileged Windows -I -B required",
    )
    expected = storage.root(BRIDGE) / "envs/bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "installed I5 private Bridge Owner interpreter required",
    )
    smoke = helper("v0250-i5-provider-installed-smoke.py", "i5_bridge_mcp_public_lifecycle")
    stage = helper("v0250-i5-bridge-public-connector-stage.py", "i5_public_exact_owners")
    PHASE = "all-five-i5-and-original-i7-disabled"
    stage.postflight()
    baseline = smoke.protected_snapshot(smoke.load_gate())
    production = storage.load("core")
    generation = _current_identity(production)
    require(generation is not None, "production process generation unknown")
    sid = storage._sid()
    keys = {}
    bindings = {}
    for core, _connector, _host, port in ROUTES:
        manifest = storage.load("core", core)
        require(manifest.settings["port"] == port, "I5 Core port drift")
        workdirs, policy = load_native_config(Path(str(manifest.settings["config"])))
        require(policy.bridge_required and len(workdirs) == 1, "I5 Core Bridge policy absent")
        bindings[core] = load_native_core_bridge_binding(manifest, workdirs)
        auth = storage.home("core", core) / "http.env"
        storage.check_private(auth, directory=False, sid=sid)
        key = _read_env_file(auth).get("SERVERFS_HTTP_API_KEY")
        require(isinstance(key, str) and len(key) >= 40, "I5 private public Bearer missing")
        keys[core] = key
    require(len(set(keys.values())) == 2, "two public Core Bearers are not independent")

    started: list[tuple[str, str, str | None]] = []
    failed: BaseException | None = None
    failed_phase = ""
    cleanup = True
    try:
        PHASE = "start-only-private-i5-Bridge"
        started.append(("bridge", BRIDGE, None))
        require(
            smoke.cli("bridge", BRIDGE, "start").get("readiness") == "owner_alive",
            "I5 Bridge not ready",
        )
        PHASE = "native-two-Core-signed-Bridge-HMAC"
        asyncio.run(asyncio.wait_for(smoke.signed_dual_core(bindings, sid), timeout=45))
        for core, _conn, _host, _port in ROUTES:
            PHASE = "start-only-" + core
            started.append(("core", core, None))
            require(
                smoke.cli("core", core, "start").get("readiness") == "verified", "I5 Core not ready"
            )
        for core, connector, _host, _port in ROUTES:
            PHASE = "start-exact-Core-bound-" + connector
            original_i7_owners_disabled()  # live-I5-safe exact original owner check
            started.append(("cloudflare", core, connector))
            require(
                connector_cli(core, connector, "start").get("readiness") == "owner_alive",
                "I5 private Connector owner not ready",
            )
        print("I5_PUBLIC_TWO_NATIVE_CORE_BOUND_CONNECTORS=PASS")
        PHASE = "public-negative-authentication-and-origin"
        for core, _conn, host, _port in ROUTES:
            foreign = ROUTES[1][0] if core == ROUTES[0][0] else ROUTES[0][0]
            asyncio.run(asyncio.wait_for(denials(host, keys[core], keys[foreign]), timeout=80))
        print("I5_PUBLIC_TWO_CORE_BEARER_ISOLATION=PASS")
        PHASE = "public-two-independent-Agent-enabled-MCP-sessions"
        asyncio.run(asyncio.wait_for(public_both(keys), timeout=110))
        PHASE = "stop-Connector-A-verify-B-peer"
        first = ROUTES[0]
        second = ROUTES[1]
        b_manifest = storage.load("cloudflare", second[0], second[1])
        bridge_manifest = storage.load("bridge", BRIDGE)
        b_generation = _current_identity(b_manifest)
        bridge_generation = _current_identity(bridge_manifest)
        require(
            b_generation is not None and bridge_generation is not None,
            "peer process generation unavailable",
        )
        require(
            is_disabled(connector_cli(first[0], first[1], "stop")),
            "Connector A scoped stop not verified",
        )
        b_now = _current_identity(b_manifest)
        bridge_now = _current_identity(bridge_manifest)
        require(
            b_now is not None
            and b_generation.pid == b_now.pid
            and b_generation.creation_time == b_now.creation_time
            and bridge_now is not None
            and bridge_generation.pid == bridge_now.pid
            and bridge_generation.creation_time == bridge_now.creation_time,
            "Connector B or Bridge changed when Connector A stopped",
        )
        asyncio.run(asyncio.wait_for(public_mcp(second[2], keys[second[0]]), timeout=80))
        print("I5_PUBLIC_B_SURVIVES_CONNECTOR_A_STOP=PASS")
        PHASE = "stop-Core-A-verify-B-and-shared-Bridge"
        require(
            is_disabled(smoke.cli("core", first[0], "stop")), "Core A did not stop independently"
        )
        asyncio.run(asyncio.wait_for(public_mcp(second[2], keys[second[0]]), timeout=80))
        print("I5_PUBLIC_B_SURVIVES_CORE_A_STOP=PASS")
    except BaseException as exc:  # noqa: BLE001 - keep sensitive data in process
        failed, failed_phase = exc, PHASE
    finally:
        PHASE = "reverse-stop-only-five-i5-components"
        for component, core, connector in reversed(started):
            try:
                status = (
                    connector_cli(core, connector, "status")
                    if component == "cloudflare"
                    else smoke.cli(component, core, "status")
                )
                if not is_disabled(status):
                    status = (
                        connector_cli(core, connector, "stop")
                        if component == "cloudflare"
                        else smoke.cli(component, core, "stop")
                    )
                cleanup = is_disabled(status) and cleanup
            except BaseException:
                cleanup = False
        del keys
    print("I5_PUBLIC_FIVE_SCOPED_TASKS_DISABLED=" + ("PASS" if cleanup else "UNVERIFIED"))
    require(cleanup, "I5 five private components not cleanly stopped")
    PHASE = "original-Tunnels-disabled-and-production-postflight"
    stage.postflight()
    now = _current_identity(production)
    require(
        now is not None
        and now.pid == generation.pid
        and now.creation_time == generation.creation_time,
        "production Core process generation changed",
    )
    require(
        smoke.protected_snapshot(smoke.load_gate()) == baseline,
        "protected Scheduler task baseline drift",
    )
    print("I5_PUBLIC_ORIGINAL_I7_AND_PRODUCTION_UNCHANGED=PASS")
    if failed is not None:
        PHASE = failed_phase
        raise failed
    print("I5_PUBLIC_TWO_CORE_SHARED_BRIDGE_REAL_HTTPS_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - print no raw exception message
        print(
            "I5_PUBLIC_BRIDGE_CANARY=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

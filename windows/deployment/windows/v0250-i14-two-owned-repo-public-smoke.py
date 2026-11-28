"""I14 one-use HTTPS/MCP smoke: two separate owned Repo-only Windows pairs.

Run ONLY after independent verified nonproduction Cloudflare Tunnel A/B ingress
handoff to i13-a.local:18883 and i13-b.local:18884, respectively, with 404
fallback. This file neither controls Cloudflare nor changes DNS, Tokens or
production. Stops every test-owned native Scheduler task on any failure.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import os
import sys
from pathlib import Path

ROUTES = (
    ("i13-a", "i14-edge-a", "workpc-v25a.ntlx.top", 18883),
    ("i13-b", "i14-edge-b", "workpc-v25b.ntlx.top", 18884),
)
PHASE = "entry"


class I14PublicRefusal(RuntimeError):
    """Nonsecret diagnostic; never expose HTTP credentials, OS paths or Git output."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I14PublicRefusal(message)


def helper(filename: str, name: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, "trusted I14 helper missing")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def disabled(state: dict) -> bool:
    return (
        state.get("registered") is True
        and state.get("enabled") is False
        and state.get("state") == "Disabled"
        and state.get("running") is False
    )


async def public_repo(host: str, own: str, foreign: str, decode) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    require(own != foreign, "different Core Bearers required")
    url = "https://" + host + "/mcp"
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "i14-native-public", "version": "1"},
        },
    }
    plain = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    async with httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=25) as negative:
        missing = await negative.post(url, headers=plain, json=init)
        wrong = await negative.post(
            url, headers={**plain, "Authorization": "Bearer invalid-i14"}, json=init
        )
        cross = await negative.post(
            url, headers={**plain, "Authorization": "Bearer " + foreign}, json=init
        )
        require(
            missing.status_code == wrong.status_code == cross.status_code == 401,
            "missing/invalid/foreign public Bearer accepted",
        )
        bad_host = await negative.post(
            url,
            headers={**plain, "Authorization": "Bearer " + own, "Host": "invalid.test"},
            json=init,
        )
        bad_origin = await negative.post(
            url,
            headers={
                **plain,
                "Authorization": "Bearer " + own,
                "Origin": "https://invalid.test",
            },
            json=init,
        )
        require(
            bad_host.status_code not in (200, 201, 202)
            and bad_origin.status_code not in (200, 201, 202),
            "untrusted Host/Origin authorized",
        )
    async with (
        httpx2.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=35,
            headers={"Authorization": "Bearer " + own},
        ) as http,
        streamable_http_client(url, http_client=http) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        names = {entry.name for entry in (await session.list_tools()).tools}
        require(
            {"list_workdirs", "inspect_repo", "run_repo_action"} <= names
            and "submit_agent_task" not in names
            and "list_agent_runtimes" not in names,
            "public Repo-only tool surface differed",
        )
        roots = decode(await session.call_tool("list_workdirs", {}))
        fixture = next(
            (
                item
                for item in roots.get("workdirs", ())
                if isinstance(item, dict) and item.get("alias") == "Fixture"
            ),
            None,
        )
        require(
            isinstance(fixture, dict)
            and fixture.get("access") == "read-write"
            and fixture.get("agent_mode") == "disabled"
            and fixture.get("repo_ops") is True,
            "public Repo-only Fixture capability mismatch",
        )
        actions = decode(
            await session.call_tool("inspect_repo", {"workdir": "Fixture", "view": "actions"})
        ).get("actions")
        require(
            isinstance(actions, list)
            and len(actions) == 1
            and isinstance(actions[0], dict)
            and actions[0].get("name") == "status_probe"
            and actions[0].get("mutates") is False,
            "public immutable Git action inventory differs",
        )
        status = decode(
            await session.call_tool("inspect_repo", {"workdir": "Fixture", "view": "status"})
        )
        require(
            status.get("staged_paths") == [] and status.get("untracked_paths") == [],
            "public Git fixture dirty",
        )
        result = decode(
            await session.call_tool(
                "run_repo_action", {"workdir": "Fixture", "action": "status_probe"}
            )
        )
        require(
            result.get("returncode") == 0
            and result.get("mutates") is False
            and result.get("action") == "status_probe",
            "public signed fixed Repo Ops denied",
        )
        unknown = await session.call_tool(
            "run_repo_action", {"workdir": "Fixture", "action": "unconfigured"}
        )
        require(
            getattr(unknown, "is_error", getattr(unknown, "isError", None)) is True,
            "unconfigured public Repo Ops action allowed",
        )


async def both(keys: dict[str, str], decode) -> None:
    a, b = ROUTES
    await asyncio.gather(
        public_repo(a[2], keys[a[0]], keys[b[0]], decode),
        public_repo(b[2], keys[b[0]], keys[a[0]], decode),
    )


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(sys.argv[1:] == ["--run-once"], "one-use I14 public canary flag required")
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "native Windows ordinary SID -I -B required",
    )
    expected = storage.root(ROUTES[0][0]) / "envs/bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "exact installed I13 private Bridge Python required",
    )
    stage = helper("v0250-i14-two-owned-repo-public-connector-stage.py", "i14_public_stage")
    decode = helper("v0250-i11-two-owned-provider-no-model-smoke.py", "i14_public_decode").data
    i5 = helper("v0250-i5-two-core-public-bridge-smoke.py", "i14_original_i7_owners")
    prior = helper("v0250-i4-independent-live-smoke.py", "i14_protected_snapshot")
    gate = helper("v0250-i4-independent-preflight.py", "i14_protected_gate")

    PHASE = "six-Disabled-test-owners-and-I7-offline"
    stage.postflight()
    require(
        {(x[0], x[1], x[3]) for x in stage.ROUTES} == {(x[0], x[1], x[3]) for x in ROUTES},
        "I14 exact staged/public routing mapping differs",
    )
    i5.original_i7_owners_disabled()
    baseline = prior.protected_baseline(gate)
    production = storage.load("core")
    generation = _current_identity(production)
    require(generation is not None, "production Core generation unavailable")
    scheduler = Scheduler()
    manifests = {}
    keys = {}
    for core, connector, _host, port in ROUTES:
        owner = storage.load("core", core)
        edge = storage.load("cloudflare", core, connector)
        require(
            owner.settings["port"] == port and edge.settings["port"] == port,
            "I14 Core/Connector port drift",
        )
        workdirs, config = load_native_config(Path(str(owner.settings["config"])))
        binding = load_native_core_bridge_binding(owner, workdirs)
        require(
            config.repo_ops_enabled
            and config.bridge_required
            and binding.bridge_mode == "owned"
            and binding.instance_id == binding.bridge_id == core,
            "I14 owned Bridge/Repo-only Core binding mismatch",
        )
        for comp, obj in (
            ("bridge", storage.load("bridge", core)),
            ("core", owner),
            ("cloudflare", edge),
        ):
            manifests[(comp, core)] = obj
            require(disabled(scheduler.status(obj)), "test native owner not Disabled")
        auth = storage.home("core", core) / "http.env"
        storage.check_private(auth, directory=False, sid=storage._sid())
        key = _read_env_file(auth).get("SERVERFS_HTTP_API_KEY")
        require(isinstance(key, str) and len(key) >= 40, "I14 Core private Bearer absent")
        keys[core] = key
    require(keys[ROUTES[0][0]] != keys[ROUTES[1][0]], "I14 Core Bearers duplicated")

    attempted = []
    failure: BaseException | None = None
    failure_phase = ""
    cleanup = True
    try:
        for component in ("bridge", "core", "cloudflare"):
            for core, _connector, _host, _port in ROUTES:
                PHASE = "start-" + component + "-" + core
                item = manifests[(component, core)]
                attempted.append(item)
                started = scheduler.start(item)
                require(
                    started.get("running") is True
                    and started.get("enabled") is True
                    and started.get("readiness")
                    == ("verified" if component == "core" else "owner_alive"),
                    "scoped native owner start not ready",
                )
        print("I14_SIX_INDEPENDENT_NATIVE_OWNER_TASKS=PASS")
        PHASE = "two-real-public-HTTPS-MCP-Repo-Ops"
        asyncio.run(asyncio.wait_for(both(keys, decode), timeout=150))
        print("I14_TWO_PUBLIC_REPO_MCP_AND_BEARER_ISOLATION=PASS")
        PHASE = "stop-A-Bridge-and-Connector-peer-survival"
        peer_core = ROUTES[1][0]
        peers = [manifests[(name, peer_core)] for name in ("bridge", "core", "cloudflare")]
        before = [_current_identity(x) for x in peers]
        require(all(x is not None for x in before), "peer B owner generation missing")
        require(
            disabled(scheduler.stop(manifests[("cloudflare", ROUTES[0][0])]))
            and disabled(scheduler.stop(manifests[("bridge", ROUTES[0][0])])),
            "A Connector and Bridge independent stop failed",
        )
        after = [_current_identity(x) for x in peers]
        require(
            all(x is not None for x in after)
            and all(
                x.pid == y.pid and x.creation_time == y.creation_time
                for x, y in zip(before, after, strict=True)
            ),
            "peer B Core/Bridge/Connector generation changed",
        )
        asyncio.run(
            asyncio.wait_for(
                public_repo(ROUTES[1][2], keys[peer_core], keys[ROUTES[0][0]], decode),
                timeout=80,
            )
        )
        print("I14_PUBLIC_B_REPO_SURVIVED_A_BRIDGE_CONNECTOR_STOP=PASS")
    except BaseException as exc:  # noqa: BLE001 - bounded phase/class only
        failure, failure_phase = exc, PHASE
    finally:
        PHASE = "stop-exact-six-attempted-owned-Tasks"
        for item in reversed(attempted):
            try:
                status = scheduler.status(item)
                if not disabled(status):
                    status = scheduler.stop(item)
                cleanup = disabled(status) and cleanup
            except BaseException:
                cleanup = False
    del keys
    print("I14_SIX_NONPROD_OWNERS_DISABLED=" + ("PASS" if cleanup else "UNVERIFIED"))
    require(cleanup, "scoped native test cleanup unverified")
    PHASE = "independent-installed-and-production-postflight"
    stage.postflight()
    after = _current_identity(production)
    require(
        after is not None
        and after.pid == generation.pid
        and after.creation_time == generation.creation_time
        and prior.protected_baseline(gate) == baseline,
        "protected production or fixture baseline drift",
    )
    print("I14_PRODUCTION_I4_I11_AND_I7=UNCHANGED")
    if failure is not None:
        PHASE = failure_phase
        raise failure
    print("I14_TWO_INDEPENDENT_OWNED_REPO_PUBLIC_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - never surface raw errors
        print(
            "I14_PUBLIC_OWNED_REPO=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

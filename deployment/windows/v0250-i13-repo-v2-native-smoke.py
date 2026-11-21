"""I13 v2 independent Repo-only owned pairs: no-file-mutation native E2E.

Runs authenticated HTTP MCP Repo Ops against two isolated local-only Git
Fixtures. Four exact current-user test owners are started/stopped natively
through Windows Scheduler; no Provider, Git mutation, Connector or production
workdir is touched. Failure phase and cleanup result stay independent.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import os
import sys
from pathlib import Path

IDS = ("i13-a", "i13-b")
PORTS = {"i13-a": 18883, "i13-b": 18884}
PHASE = "entry"


class I13RepoV2Refusal(RuntimeError):
    """All diagnostics use bounded hardcoded markers, no untrusted content."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I13RepoV2Refusal(reason)


def helper(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    require(spec is not None and spec.loader is not None, "trusted native helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def probe(instance: str, own_key: str, foreign_key: str, decode) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    require(instance in IDS and own_key != foreign_key, "private Core identity overlap")
    url = f"http://127.0.0.1:{PORTS[instance]}/mcp"
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "i13-repo-v2-native", "version": "1"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + foreign_key,
    }
    async with httpx2.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as negative:
        unauthorized = await negative.post(url, headers=headers, json=init)
        require(unauthorized.status_code == 401, "foreign HTTP Bearer was accepted")

    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + own_key},
            timeout=35,
            trust_env=False,
            follow_redirects=False,
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
            "Repo-only MCP surface missing or widened",
        )
        roots = decode(await session.call_tool("list_workdirs", {}))
        fixture = next(
            (
                x
                for x in roots.get("workdirs", ())
                if isinstance(x, dict) and x.get("alias") == "Fixture"
            ),
            None,
        )
        require(
            isinstance(fixture, dict)
            and fixture.get("access") == "read-write"
            and fixture.get("agent_mode") == "disabled"
            and fixture.get("repo_ops") is True,
            "private Fixture capabilities diverged",
        )
        actions = decode(
            await session.call_tool("inspect_repo", {"workdir": "Fixture", "view": "actions"})
        )
        rows = actions.get("actions")
        require(
            isinstance(rows, list)
            and len(rows) == 1
            and isinstance(rows[0], dict)
            and rows[0].get("name") == "status_probe"
            and rows[0].get("mutates") is False,
            "fixed immutable repo action inventory differs",
        )
        status = decode(
            await session.call_tool("inspect_repo", {"workdir": "Fixture", "view": "status"})
        )
        require(
            status.get("staged_paths") == [] and status.get("untracked_paths") == [],
            "isolated nonproduction Git fixture was dirty",
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
            "signed owned Bridge failed fixed read-only Git action",
        )
        denied = await session.call_tool(
            "run_repo_action", {"workdir": "Fixture", "action": "unknown_command"}
        )
        require(
            getattr(denied, "is_error", getattr(denied, "isError", None)) is True,
            "unconfigured Repo action was accepted",
        )


async def both(keys: dict[str, str], decode) -> None:
    await asyncio.gather(
        probe(IDS[0], keys[IDS[0]], keys[IDS[1]], decode),
        probe(IDS[1], keys[IDS[1]], keys[IDS[0]], decode),
    )


def disabled(state: dict) -> bool:
    return (
        state.get("registered") is True
        and state.get("enabled") is False
        and state.get("state") == "Disabled"
        and state.get("running") is False
    )


def main() -> None:
    global PHASE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import cli, storage
    from serverfs_mcp.windows_v25.legacy_bridge_ownership import (
        assert_legacy_writable_roots_disjoint,
    )
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(sys.argv[1:] == ["--run-once"], "exact one-use Repo-only flag required")
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "native Windows ordinary SID -I -B required",
    )
    expected = storage.root(IDS[0]) / "envs/bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "i13-a exact installed Bridge Python required",
    )
    stage = helper("v0250-i13-owned-repo-stage.py", "i13_repo_v2_installed")
    prior = helper("v0250-i4-independent-live-smoke.py", "i13_repo_v2_protection")
    gate = helper("v0250-i4-independent-preflight.py", "i13_repo_v2_host")
    decode = helper("v0250-i11-two-owned-provider-no-model-smoke.py", "i13_repo_v2_decode").data
    PHASE = "four-disabled-owners-and-protected-baseline"
    stage.postflight()
    baseline = prior.protected_baseline(gate)
    production = storage.load("core")
    generation = _current_identity(production)
    require(generation is not None, "protected production generation unavailable")
    scheduler = Scheduler()
    manifests = {}
    keys = {}
    for instance in IDS:
        for component in ("bridge", "core"):
            manifest = storage.load(component, instance)
            cli._preflight(manifest)
            cli._assert_instance_isolation(manifest)
            if component == "bridge":
                assert_legacy_writable_roots_disjoint(manifest)
            require(disabled(scheduler.status(manifest)), "owner not initially Disabled")
            manifests[(component, instance)] = manifest
        auth = storage.home("core", instance) / "http.env"
        storage.check_private(auth, directory=False, sid=storage._sid())
        key = _read_env_file(auth).get("SERVERFS_HTTP_API_KEY")
        require(isinstance(key, str) and len(key) >= 40, "private Bearer is unavailable")
        keys[instance] = key
    require(keys[IDS[0]] != keys[IDS[1]], "two Core Bearers overlap")

    attempted = []
    failure: BaseException | None = None
    failed_phase = ""
    cleanup_ok = True
    try:
        for component in ("bridge", "core"):
            for instance in IDS:
                PHASE = "start-" + component + "-" + instance
                manifest = manifests[(component, instance)]
                attempted.append(manifest)
                started = scheduler.start(manifest)
                require(
                    started.get("running") is True
                    and started.get("enabled") is True
                    and started.get("readiness")
                    == ("verified" if component == "core" else "owner_alive"),
                    "native Scheduler owner did not become ready",
                )
                print(
                    "I13_REPO_V2_"
                    + component.upper()
                    + "_"
                    + instance.upper().replace("-", "_")
                    + "_START=PASS"
                )
        PHASE = "two-concurrent-authenticated-fixed-Repo-Ops"
        asyncio.run(asyncio.wait_for(both(keys, decode), timeout=100))
        print("I13_REPO_V2_TWO_AUTHENTICATED_FIXED_GIT_ACTIONS=PASS")
        print("I13_REPO_V2_CROSS_BEARER_AND_UNKNOWN_ACTION=DENIED")
        PHASE = "stop-only-A-Bridge-and-test-B-Repo-survival"
        b_core = manifests[("core", IDS[1])]
        b_bridge = manifests[("bridge", IDS[1])]
        before = (_current_identity(b_core), _current_identity(b_bridge))
        require(all(item is not None for item in before), "peer B owner generations missing")
        require(
            disabled(scheduler.stop(manifests[("bridge", IDS[0])])),
            "A Bridge could not stop independently",
        )
        after = (_current_identity(b_core), _current_identity(b_bridge))
        require(
            all(prior.same_generation(x, y) for x, y in zip(before, after, strict=True)),
            "peer B generation changed after stopping Bridge A",
        )
        asyncio.run(asyncio.wait_for(probe(IDS[1], keys[IDS[1]], keys[IDS[0]], decode), timeout=50))
        print("I13_REPO_V2_PEER_B_REPO_SURVIVED_A_BRIDGE_STOP=PASS")
    except BaseException as exc:  # noqa: BLE001
        failure, failed_phase = exc, PHASE
        print("I13_REPO_V2_OPERATION=FAILED phase=" + failed_phase + " type=" + type(exc).__name__)
    finally:
        for manifest in reversed(attempted):
            label = (
                "I13_REPO_V2_CLEANUP_"
                + manifest.component.upper()
                + "_"
                + manifest.instance_id.upper().replace("-", "_")
            )
            try:
                result = scheduler.stop(manifest)
                ok = disabled(result) and _current_identity(manifest) is None
                cleanup_ok = ok and cleanup_ok
                print(label + ("=PASS" if ok else "=UNVERIFIED"))
            except BaseException:
                cleanup_ok = False
                print(label + "=UNVERIFIED")
    del keys
    print("I13_REPO_V2_ALL_ATTEMPTED_OWNERS_STOPPED=" + ("PASS" if cleanup_ok else "UNVERIFIED"))
    require(cleanup_ok, "one or more exact test owners not proven disabled")
    PHASE = "final-protected-native-postflight"
    stage.postflight()
    require(
        prior.same_generation(generation, _current_identity(production))
        and prior.protected_baseline(gate) == baseline,
        "protected production or prior owner baseline changed",
    )
    print("I13_REPO_V2_PRODUCTION_AND_I4_I11=UNCHANGED")
    if failure is not None:
        PHASE = failed_phase
        raise I13RepoV2Refusal("observed Repo operation failure") from None
    print("I13_REPO_V2_TWO_INDEPENDENT_NATIVE_REPO_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001
        print(
            "I13_REPO_V2=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

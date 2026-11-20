"""I11 single-use Windows native two independent Claude-capable owned-pair canary.

Real installed Windows Task Scheduler, signed Named Pipes, two authenticated
HTTP MCP Core endpoints. Tests independently discover Claude runtime through
both isolated Bridges, cross-Core bearer/HMAC refusal and one Bridge stop/B
survival. Never submits an Agent task or invokes Claude Provider; no Repo
Ops, no file write, no Tunnel or production service mutation.

Four scoped test tasks are stopped in finally. Exact private interpreter and
current SID required; sanitized status only.
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

IDS = ("i11-a", "i11-b")
PORTS = {"i11-a": 18881, "i11-b": 18882}
TASKS = tuple((component, name) for component in ("bridge", "core") for name in IDS)
PHASE = "entry"


class I11RealRuntimeRefusal(RuntimeError):
    """No provider payload, keys, private path or command stderr in output."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I11RealRuntimeRefusal(reason)


def helper(filename: str, label: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, path)
    require(spec is not None and spec.loader is not None, "I11 approved source helper missing")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def is_disabled(status: dict) -> bool:
    return (
        status.get("registered") is True
        and status.get("enabled") is False
        and status.get("running") is False
        and status.get("state") == "Disabled"
    )


def same(a, b) -> bool:
    return a is not None and b is not None and a.pid == b.pid and a.creation_time == b.creation_time


def cli(component: str, instance: str, operation: str) -> dict:
    from serverfs_mcp.windows_v25 import storage

    require(
        (component, instance) in TASKS and operation in ("start", "stop", "status"),
        "I11 lifecycle outside two owned pairs",
    )
    interpreter = storage.root(instance) / "envs" / component / "Scripts/python.exe"
    storage.check_private(interpreter, directory=False, sid=storage._sid())
    proc = subprocess.run(
        [
            str(interpreter),
            "-I",
            "-B",
            "-m",
            "serverfs_mcp.windows_v25.cli",
            component,
            operation,
            "--instance",
            instance,
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=70,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(proc.returncode == 0, "I11 scoped native lifecycle CLI returned nonzero")
    try:
        response = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise I11RealRuntimeRefusal("I11 scoped CLI status malformed") from None
    require(isinstance(response, dict), "I11 scoped CLI status invalid")
    return response


def data(response) -> dict:
    require(
        getattr(response, "is_error", getattr(response, "isError", True)) is False,
        "I11 authentic MCP tool returned error",
    )
    value = getattr(response, "structured_content", None)
    if value is None:
        value = getattr(response, "structuredContent", None)
    if not isinstance(value, dict):
        content = getattr(response, "content", ())
        require(
            bool(content) and getattr(content[0], "type", "") == "text",
            "I11 SDK result body unavailable",
        )
        try:
            value = json.loads(content[0].text)
        except (UnicodeError, ValueError):
            raise I11RealRuntimeRefusal("I11 SDK result not JSON") from None
    require(isinstance(value, dict), "I11 SDK result invalid")
    row = value.get("result", value)
    require(isinstance(row, dict), "I11 SDK result payload invalid")
    return row


async def signed_two_owned_bridges(bindings, sid: str) -> None:
    from serverfs_mcp.agent_client import AgentBridgeRemoteError
    from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient
    from serverfs_mcp.windows_v25.bridge_peer_job import make_bridge_peer_validator

    require(set(bindings) == set(IDS), "I11 two signed bindings missing")
    for instance in IDS:
        binding = bindings[instance]
        require(
            binding.bridge_mode == "owned" and binding.bridge_id == binding.instance_id == instance,
            "I11 binding is not owned by exact Core",
        )
        peer = make_bridge_peer_validator(bridge_id=instance, user_sid=sid)
        _settings, signed = binding.build_wiring(peer_validator=peer)
        pong = await signed.call("bridge.ping", {"workdir": "Fixture"})
        require(pong == {"status": "ok"}, "I11 signed owned Pipe ping refused")
        runtime = await signed.call("runtime.list", {})
        require(
            isinstance(runtime, dict)
            and isinstance(runtime.get("runtimes"), list)
            and any(
                isinstance(item, dict)
                and item.get("name") == "claude"
                and item.get("available") is True
                for item in runtime["runtimes"]
            ),
            "I11 isolated owned Claude Worker not available",
        )
    print("I11_TWO_REAL_SIGNED_OWNED_CLAUDE_PIPES=PASS")

    a, b = bindings[IDS[0]], bindings[IDS[1]]
    peer_a = make_bridge_peer_validator(bridge_id=IDS[0], user_sid=sid)
    forged = CoreGrantBridgeClient(
        Path(a.pipe_name),
        core_id=IDS[0],
        bridge_id=a.bridge_id,
        key_id=b.key_id,
        secret=b.secret,
        policy_revisions=dict(a.policy_revisions),
        server_pid_validator=peer_a,
    )
    try:
        await forged.call("bridge.ping", {"workdir": "Fixture"})
    except AgentBridgeRemoteError as exc:
        require(exc.code == "CORE_NOT_AUTHORIZED", "I11 cross-Bridge refusal code differs")
    else:
        raise I11RealRuntimeRefusal("I11 Bridge A accepted Bridge B HMAC key")
    print("I11_FOREIGN_OWNED_BRIDGE_HMAC=DENIED")


async def http_core(instance: str, token: str, foreign_token: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    require(instance in IDS and token != foreign_token, "I11 Core credentials are not unique")
    url = f"http://127.0.0.1:{PORTS[instance]}/mcp"
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "i11-installed-nonprod", "version": "1"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    async with httpx2.AsyncClient(trust_env=False, timeout=15, follow_redirects=False) as client:
        wrong = await client.post(
            url, headers={**headers, "Authorization": "Bearer invalid-i11"}, json=request
        )
        cross = await client.post(
            url,
            headers={**headers, "Authorization": "Bearer " + foreign_token},
            json=request,
        )
        require(
            wrong.status_code == cross.status_code == 401,
            "I11 Core accepted invalid or other Core Bearer",
        )
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
            follow_redirects=False,
            timeout=30,
        ) as http,
        streamable_http_client(url, http_client=http) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        names = {item.name for item in (await session.list_tools()).tools}
        require(
            {"list_workdirs", "list_agent_runtimes", "submit_agent_task", "get_agent_task"} <= names
            and "run_repo_action" not in names,
            "I11 Core Agent-only MCP surface differs",
        )
        roots = data(await session.call_tool("list_workdirs", {}))
        fixture = next(
            (
                item
                for item in roots.get("workdirs", ())
                if isinstance(item, dict) and item.get("alias") == "Fixture"
            ),
            None,
        )
        require(
            fixture is not None
            and fixture.get("access") == "read-write"
            and fixture.get("agent_mode") == "workspace-write"
            and fixture.get("repo_ops") is False,
            "I11 dedicated writable Fixture policy changed",
        )
        runtimes = data(await session.call_tool("list_agent_runtimes", {}))
        require(
            isinstance(runtimes.get("runtimes"), list)
            and any(
                isinstance(item, dict)
                and item.get("name") == "claude"
                and item.get("available") is True
                for item in runtimes["runtimes"]
            ),
            "I11 Core cannot discover its own Claude Worker over owned Bridge",
        )


async def both_cores(keys: dict[str, str]) -> None:
    await asyncio.gather(
        http_core(IDS[0], keys[IDS[0]], keys[IDS[1]]),
        http_core(IDS[1], keys[IDS[1]], keys[IDS[0]]),
    )


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(sys.argv[1:] == ["--run-once"], "I11 one-use no-model canary required")
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "native non-admin Windows -I -B required",
    )
    installed = storage.root(IDS[0]) / "envs/bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == installed.resolve(strict=True),
        "exact installed I11 A Bridge Owner Python required",
    )
    stage = helper("v0250-i11-two-owned-provider-stage.py", "i11_live_disabled_scope")
    PHASE = "private-four-disabled-owner-and-production-preflight"
    stage.postflight()
    i4 = helper("v0250-i4-independent-live-smoke.py", "i11_protected_source")
    pre = helper("v0250-i4-independent-preflight.py", "i11_protected_host")
    baseline = i4.protected_baseline(pre)
    production = storage.load("core")
    generation = _current_identity(production)
    require(generation is not None, "I11 production Core generation unavailable")
    bindings = {}
    keys: dict[str, str] = {}
    for instance in IDS:
        manifest = storage.load("core", instance)
        workdirs, settings = load_native_config(Path(str(manifest.settings["config"])))
        require(
            settings.bridge_required
            and len(workdirs) == 1
            and manifest.settings["port"] == PORTS[instance],
            "I11 Core private plan mismatch",
        )
        bindings[instance] = load_native_core_bridge_binding(manifest, workdirs)
        auth = storage.home("core", instance) / "http.env"
        storage.check_private(auth, directory=False, sid=storage._sid())
        key = _read_env_file(auth).get("SERVERFS_HTTP_API_KEY")
        require(isinstance(key, str) and len(key) >= 40, "I11 private Core Bearer invalid")
        keys[instance] = key
    require(keys[IDS[0]] != keys[IDS[1]], "I11 two Core Bearers overlap")

    started: list[tuple[str, str]] = []
    failure: BaseException | None = None
    failed_phase = ""
    cleaned = True
    try:
        for instance in IDS:
            PHASE = "start-only-native-owned-Bridge-" + instance
            started.append(("bridge", instance))
            require(
                cli("bridge", instance, "start").get("readiness") == "owner_alive",
                "I11 owned Bridge startup not verified",
            )
        PHASE = "two-signed-owned-pipes-and-cross-Bridge-HMAC-refusal"
        asyncio.run(
            asyncio.wait_for(signed_two_owned_bridges(bindings, storage._sid()), timeout=50)
        )
        for instance in IDS:
            PHASE = "start-only-native-Core-" + instance
            started.append(("core", instance))
            require(
                cli("core", instance, "start").get("readiness") == "verified",
                "I11 independent Core not ready",
            )
        PHASE = "two-authenticated-agent-enabled-HTTP-MCP"
        asyncio.run(asyncio.wait_for(both_cores(keys), timeout=110))
        print("I11_TWO_CONCURRENT_OWNED_CORE_CLAUDE_MCP=PASS")
        PHASE = "stop-Bridge-A-Core-and-other-pair-survival"
        bridge_a = storage.load("bridge", IDS[0])
        core_a = storage.load("core", IDS[0])
        bridge_b = storage.load("bridge", IDS[1])
        core_b = storage.load("core", IDS[1])
        before = (
            _current_identity(core_a),
            _current_identity(bridge_b),
            _current_identity(core_b),
        )
        require(all(item is not None for item in before), "I11 peer generations missing")
        require(is_disabled(cli("bridge", IDS[0], "stop")), "I11 Bridge A scope stop refused")
        require(_current_identity(bridge_a) is None, "I11 Bridge A process remains resident")
        after = (
            _current_identity(core_a),
            _current_identity(bridge_b),
            _current_identity(core_b),
        )
        require(
            all(same(old, new) for old, new in zip(before, after, strict=True)),
            "I11 stopping Bridge A changed independent Core/Bridge generations",
        )
        asyncio.run(
            asyncio.wait_for(
                http_core(IDS[1], keys[IDS[1]], keys[IDS[0]]),
                timeout=70,
            )
        )
        print("I11_BRIDGE_B_AND_CORE_B_SURVIVE_BRIDGE_A_STOP=PASS")
    except BaseException as exc:  # noqa: BLE001 - raw evidence stays local
        failure, failed_phase = exc, PHASE
    finally:
        PHASE = "scoped-reverse-stop-four-I11-native-owners"
        for component, instance in reversed(started):
            try:
                status = cli(component, instance, "status")
                if not is_disabled(status):
                    status = cli(component, instance, "stop")
                cleaned = is_disabled(status) and cleaned
            except BaseException:
                cleaned = False
    del keys
    print("I11_ALL_FOUR_SCOPED_TASKS_DISABLED=" + ("PASS" if cleaned else "UNVERIFIED"))
    require(cleaned, "I11 exact I11 cleanup not verified")
    PHASE = "postflight-protected-current-production-generation"
    stage.postflight()
    now = _current_identity(production)
    require(
        same(generation, now) and baseline == i4.protected_baseline(pre),
        "I11 production/I4 protected Scheduler or owner generation drift",
    )
    print("I11_PRODUCTION_I4_AND_I7_NATIVE_BASELINES=UNCHANGED")
    if failure is not None:
        PHASE = failed_phase
        raise failure
    print("I11_TWO_INDEPENDENT_OWNED_CLAUDE_CORES_NATIVE_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - only phase/type may be printed
        print(
            "I11_TWO_OWNED_NO_MODEL_SMOKE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

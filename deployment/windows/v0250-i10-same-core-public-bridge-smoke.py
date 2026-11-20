"""I10 single-Core Bridge-backed dual Tunnel public HTTPS/MCP nonproduction canary.

Prerequisite: both nonproduction Cloudflare A/B Tunnel ingress configurations
have been independently set/read back to 127.0.0.1:18873 with canonical local
Host i5-core-a.local and a deny-all 404 fallback. No Cloudflare API/DNS call
in this script. Starts/stops only the test's two I10 Connector owners, the
existing I5 Core A and its shared Bridge; original I7 owners stay Disabled.
Never sends Provider, Repo Ops, file mutation or credential output.
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

CORE = "i5-core-a"
BRIDGE = "i5-shared"
PORT = 18873
ROUTES = (("i10-edge-a", "workpc-v25a.ntlx.top"), ("i10-edge-b", "workpc-v25b.ntlx.top"))
PHASE = "entry"


class I10PublicRefusal(RuntimeError):
    """Nonsecret refusal with only phase and error class released."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I10PublicRefusal(reason)


def helper(filename: str, label: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, path)
    require(spec is not None and spec.loader is not None, "fixed helper absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_disabled(row: dict) -> bool:
    return (
        row.get("registered") is True
        and row.get("enabled") is False
        and row.get("running") is False
        and row.get("state") == "Disabled"
    )


def connector_cli(connector: str, action: str) -> dict:
    from serverfs_mcp.windows_v25 import storage

    require(
        connector in {name for name, _host in ROUTES} and action in ("start", "stop", "status"),
        "Connector lifecycle beyond I10 scope",
    )
    python = storage.root(CORE) / "envs/cloudflare/Scripts/python.exe"
    storage.check_private(python, directory=False, sid=storage._sid())
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "serverfs_mcp.windows_v25.cli",
            "cloudflare",
            action,
            "--instance",
            CORE,
            "--connector-id",
            connector,
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=75,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0, "I10 Connector native CLI failed")
    try:
        reply = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise I10PublicRefusal("I10 Connector native status malformed") from None
    require(isinstance(reply, dict), "I10 Connector status not mapping")
    return reply


async def two_public_sessions(public, key: str) -> None:
    await asyncio.gather(*(public.public_mcp(host, key) for _name, host in ROUTES))


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(sys.argv[1:] == ["--run-once"], "I10 single-use public canary required")
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "Windows native nonadmin -I -B required",
    )
    expected = storage.root(BRIDGE) / "envs/bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I10 requires installed I5 private Bridge Owner interpreter",
    )
    stage = helper("v0250-i10-same-core-dual-connectors-stage.py", "i10_live_stage")
    public = helper("v0250-i5-two-core-public-bridge-smoke.py", "i10_public_http")
    native = helper("v0250-i5-provider-installed-smoke.py", "i10_native_lifecycle")
    PHASE = "all-four-nonproduction-owners-disabled-before-start"
    stage.postflight()
    require(
        stage.CORE == CORE
        and stage.BRIDGE == BRIDGE
        and tuple(name for name, _original in stage.CONNECTORS)
        == tuple(name for name, _host in ROUTES),
        "I10 stage identities drift",
    )
    protected_snapshot = native.protected_snapshot(native.load_gate())
    production = storage.load("core")
    original = _current_identity(production)
    require(original is not None, "production generation unavailable")

    manifest = storage.load("core", CORE)
    require(manifest.settings["port"] == PORT, "I10 Core port mismatch")
    workdirs, policy = load_native_config(Path(str(manifest.settings["config"])))
    require(policy.bridge_required and len(workdirs) == 1, "I10 Core must be Bridge-backed")
    binding = load_native_core_bridge_binding(manifest, workdirs)
    require(
        binding.instance_id == CORE
        and binding.bridge_id == BRIDGE
        and binding.bridge_mode == "shared",
        "I10 signed shared-Bridge binding drift",
    )
    source = storage.home("core", CORE) / "http.env"
    peer = storage.home("core", "i5-core-b") / "http.env"
    for path in (source, peer):
        storage.check_private(path, directory=False, sid=storage._sid())
    key = _read_env_file(source).get("SERVERFS_HTTP_API_KEY")
    foreign = _read_env_file(peer).get("SERVERFS_HTTP_API_KEY")
    require(
        isinstance(key, str)
        and isinstance(foreign, str)
        and len(key) >= 40
        and len(foreign) >= 40
        and key != foreign,
        "I10 distinct Core Bearers unavailable",
    )

    started: list[tuple[str, str]] = []
    failed: BaseException | None = None
    failed_phase = ""
    clean = True
    try:
        PHASE = "start-private-I5-shared-Bridge"
        started.append(("bridge", BRIDGE))
        require(
            native.cli("bridge", BRIDGE, "start").get("readiness") == "owner_alive",
            "I10 shared Bridge start refused",
        )
        PHASE = "signed-Core-A-Bridge-IPC"
        _, signed = binding.build_wiring()
        pong = asyncio.run(
            asyncio.wait_for(signed.call("bridge.ping", {"workdir": "Fixture"}), timeout=25)
        )
        require(pong == {"status": "ok"}, "I10 Core A signed Bridge request denied")
        print("I10_SIGNED_ONE_CORE_SHARED_BRIDGE=PASS")
        PHASE = "start-only-I5-Core-A"
        started.append(("core", CORE))
        require(
            native.cli("core", CORE, "start").get("readiness") == "verified",
            "I10 Core A not ready",
        )
        for connector, _host in ROUTES:
            PHASE = "start-only-" + connector
            public.original_i7_owners_disabled()
            started.append(("cloudflare", connector))
            require(
                connector_cli(connector, "start").get("readiness") == "owner_alive",
                "I10 Connector not ready",
            )
        print("I10_TWO_INDEPENDENT_NATIVE_CONNECTOR_OWNERS=PASS")
        PHASE = "HTTPS-authorization-host-origin-negatives"
        for _conn, hostname in ROUTES:
            asyncio.run(asyncio.wait_for(public.denials(hostname, key, foreign), timeout=80))
        print("I10_TWO_PUBLIC_HTTPS_FOREIGN_BEARERS_DENIED=PASS")
        PHASE = "concurrent-external-MCP-Bridge-backed-sessions"
        asyncio.run(asyncio.wait_for(two_public_sessions(public, key), timeout=125))
        print("I10_TWO_PUBLIC_MCP_SESSIONS_SAME_CORE_BRIDGE=PASS")
        PHASE = "A-Connector-stop-B-owner-Core-and-Bridge-survive"
        peer_conn = storage.load("cloudflare", CORE, ROUTES[1][0])
        peers = (peer_conn, manifest, storage.load("bridge", BRIDGE))
        before = [_current_identity(x) for x in peers]
        require(all(x is not None for x in before), "I10 peer generation unavailable")
        require(is_disabled(connector_cli(ROUTES[0][0], "stop")), "I10 A stop failed")
        after = [_current_identity(x) for x in peers]
        require(
            all(x is not None for x in after)
            and all(
                left.pid == right.pid and left.creation_time == right.creation_time
                for left, right in zip(before, after, strict=True)
            ),
            "I10 Connector B/Core/Bridge generation changed",
        )
        asyncio.run(asyncio.wait_for(public.public_mcp(ROUTES[1][1], key), timeout=85))
        print("I10_ROUTE_B_SURVIVES_CONNECTOR_A_STOP=PASS")
    except BaseException as exc:  # noqa: BLE001 - suppress raw provider/network values
        failed, failed_phase = exc, PHASE
    finally:
        PHASE = "reverse-stop-only-four-test-owners"
        for component, instance in reversed(started):
            try:
                state = (
                    connector_cli(instance, "status")
                    if component == "cloudflare"
                    else native.cli(component, instance, "status")
                )
                if not is_disabled(state):
                    state = (
                        connector_cli(instance, "stop")
                        if component == "cloudflare"
                        else native.cli(component, instance, "stop")
                    )
                clean = is_disabled(state) and clean
            except BaseException:
                clean = False
    del key, foreign
    print("I10_FOUR_SCOPED_TASKS_DISABLED=" + ("PASS" if clean else "UNVERIFIED"))
    require(clean, "I10 scoped cleanup could not be confirmed")
    PHASE = "protected-offline-postflight"
    stage.postflight()
    now = _current_identity(production)
    require(
        now is not None
        and now.pid == original.pid
        and now.creation_time == original.creation_time
        and native.protected_snapshot(native.load_gate()) == protected_snapshot,
        "I10 protected task/process baseline drift",
    )
    print("I10_I7_I5_CORE_B_PRODUCTION_UNCHANGED=PASS")
    if failed is not None:
        PHASE = failed_phase
        raise failed
    print("I10_TWO_PUBLIC_CONNECTORS_ONE_CORE_AND_BRIDGE_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - nonsecret class/phase only
        print(
            "I10_PUBLIC_TOPOLOGY_A=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

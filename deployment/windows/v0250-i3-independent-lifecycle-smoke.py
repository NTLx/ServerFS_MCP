"""I3: Native independent lifecycle acceptance for one shared Bridge and two Cores.

Run exactly once after the completed I3 read-only stage/smoke, while all three
I3 tasks are Disabled. Start only these three fixtures, prove that Core A can
restart without affecting Core B/Bridge, and that the Bridge can stop/restart
without restarting either HTTP Core. Verify fresh authenticated v2 Pipe/Job
health after the Bridge restarts. Scoped finally stops all I3 fixtures.

No production/legacy task, provider runtime, Repo Ops or grant file mutation.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import os
import sys
from pathlib import Path

BRIDGE = "i3-shared"
CORE_A = "i3-core-a"
CORE_B = "i3-core-b"
CORE_PORTS = {CORE_A: 18869, CORE_B: 18870}
PHASE = "entry"


class I3RestartError(RuntimeError):
    """Only redacted, fixture-scoped lifecycle/identity failure."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I3RestartError(reason)


def phase(value: str) -> None:
    global PHASE
    PHASE = value


def original_smoke():
    """Use the *reviewed*, already native-accepted I3 helper, never its main."""
    path = Path(__file__).with_name("v0250-i3-readonly-live-smoke.py")
    spec = importlib.util.spec_from_file_location("i3_restart_shared_helper", path)
    require(spec is not None and spec.loader is not None, "I3 reviewed helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(
        module.BRIDGE == BRIDGE and module.CORE_PORTS == CORE_PORTS,
        "I3 helper instance identities changed",
    )
    return module


def same_generation(left, right) -> bool:
    return (
        left is not None
        and right is not None
        and left.pid == right.pid
        and left.creation_time == right.creation_time
    )


def new_generation(left, right) -> bool:
    return (
        left is not None
        and right is not None
        and (left.pid != right.pid or left.creation_time != right.creation_time)
    )


def require_active(helper, component: str, instance: str) -> None:
    status = helper.cli(component, instance, "status")
    readiness = "owner_alive" if component == "bridge" else "verified"
    require(
        status.get("running") is True
        and status.get("enabled") is True
        and status.get("readiness") == readiness,
        "one I3 task is not independently ready",
    )


def require_disabled(helper, component: str, instance: str) -> None:
    result = helper.cli(component, instance, "status")
    require(
        result.get("state") == "Disabled"
        and result.get("running") is False
        and result.get("enabled") is False
        and result.get("readiness") == "offline",
        "one I3 fixture is not disabled/offline",
    )


def native_main() -> None:
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity

    phase("native-private-owner")
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I3 non-elevated native Python -I -B required",
    )
    owner_python = storage.root(BRIDGE) / "envs" / "bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == owner_python.resolve(strict=True),
        "I3 restart test must use the installed private Bridge owner Python",
    )
    helper = original_smoke()
    preflight = helper.gate()
    phase("production-readonly-baseline")
    production_tasks = helper.production_snapshot(preflight)
    incumbent = storage.load("core")
    production_before = _current_identity(incumbent)
    require(production_before is not None, "production Core generation is unverified")
    phase("three-disabled-preflight")
    helper.three_task_states(preflight)

    bridge = storage.load("bridge", BRIDGE)
    load_native_bridge_plan(bridge)
    cmanifests = {core_id: storage.load("core", core_id) for core_id in CORE_PORTS}
    cbinding = {}
    tokens = {}
    for core_id, manifest in cmanifests.items():
        workdirs, settings = load_native_config(Path(str(manifest.settings["config"])))
        require(settings.bridge_required, "I3 Core Bridge requirement drifted")
        cbinding[core_id] = load_native_core_bridge_binding(manifest, workdirs)
        private_auth = storage.home("core", core_id) / "http.env"
        storage.check_private(private_auth, directory=False, sid=current_user_sid())
        token = _read_env_file(private_auth).get("SERVERFS_HTTP_API_KEY")
        require(isinstance(token, str) and len(token) >= 32, "I3 bearer key unavailable")
        tokens[core_id] = token
    require(len(set(tokens.values())) == 2, "I3 Core HTTP keys are not independent")

    live: list[tuple[str, str]] = []
    failed: BaseException | None = None
    failed_phase = ""
    all_stopped = False
    try:
        phase("start-bridge")
        live.append(("bridge", BRIDGE))
        helper.cli("bridge", BRIDGE, "start")
        require_active(helper, "bridge", BRIDGE)
        phase("start-core-a")
        live.append(("core", CORE_A))
        helper.cli("core", CORE_A, "start")
        require_active(helper, "core", CORE_A)
        phase("start-core-b")
        live.append(("core", CORE_B))
        helper.cli("core", CORE_B, "start")
        require_active(helper, "core", CORE_B)
        initial_bridge = _current_identity(bridge)
        initial_a = _current_identity(cmanifests[CORE_A])
        initial_b = _current_identity(cmanifests[CORE_B])
        require(
            initial_bridge is not None and initial_a is not None and initial_b is not None,
            "I3 original process generations cannot be attributed",
        )
        asyncio.run(asyncio.wait_for(helper.shared_bridge_rpc(cbinding), timeout=28))
        print("I3_RESTART_INITIAL_SHARED_PIPE=PASS")

        # Only Core A goes down; Bridge and Core B must retain exact PIDs + birth times.
        phase("core-a-independent-stop")
        helper.cli("core", CORE_A, "stop")
        require_disabled(helper, "core", CORE_A)
        live.remove(("core", CORE_A))
        require(
            same_generation(initial_bridge, _current_identity(bridge))
            and same_generation(initial_b, _current_identity(cmanifests[CORE_B])),
            "Core A stop changed another owner's process generation",
        )
        require_active(helper, "core", CORE_B)
        phase("core-a-independent-restart")
        live.append(("core", CORE_A))
        helper.cli("core", CORE_A, "start")
        require_active(helper, "core", CORE_A)
        current_a = _current_identity(cmanifests[CORE_A])
        require(
            new_generation(initial_a, current_a)
            and same_generation(initial_b, _current_identity(cmanifests[CORE_B]))
            and same_generation(initial_bridge, _current_identity(bridge)),
            "independent Core A restart altered another owner",
        )
        awaitable = helper.public_core_check(CORE_B, CORE_PORTS[CORE_B], tokens[CORE_B])
        asyncio.run(asyncio.wait_for(awaitable, timeout=45))
        print("I3_CORE_A_RESTART_CORE_B_UNCHANGED=PASS")

        # Both Cores remain up while Bridge v2 is stopped. Core file-only tools
        # still work because they are isolated from the Bridge lifecycle.
        phase("bridge-independent-stop")
        helper.cli("bridge", BRIDGE, "stop")
        require_disabled(helper, "bridge", BRIDGE)
        live.remove(("bridge", BRIDGE))
        require(
            same_generation(current_a, _current_identity(cmanifests[CORE_A]))
            and same_generation(initial_b, _current_identity(cmanifests[CORE_B])),
            "Bridge stop terminated an independently owned Core",
        )
        require_active(helper, "core", CORE_A)
        require_active(helper, "core", CORE_B)
        for core_id, port in CORE_PORTS.items():
            phase("http-with-bridge-down-" + core_id)
            asyncio.run(
                asyncio.wait_for(
                    helper.public_core_check(core_id, port, tokens[core_id]), timeout=45
                )
            )
        print("I3_BRIDGE_DOWN_CORES_HTTP_UNCHANGED=PASS")

        phase("bridge-independent-restart")
        live.insert(0, ("bridge", BRIDGE))
        helper.cli("bridge", BRIDGE, "start")
        require_active(helper, "bridge", BRIDGE)
        current_bridge = _current_identity(bridge)
        require(
            new_generation(initial_bridge, current_bridge)
            and same_generation(current_a, _current_identity(cmanifests[CORE_A]))
            and same_generation(initial_b, _current_identity(cmanifests[CORE_B])),
            "Bridge restart changed an independent Core's process generation",
        )
        phase("signed-shared-pipe-after-bridge-restart")
        asyncio.run(asyncio.wait_for(helper.shared_bridge_rpc(cbinding), timeout=28))
        print("I3_BRIDGE_RESTART_CORES_UNCHANGED_SIGNED_PIPE=PASS")
    except BaseException as exc:
        failed = exc
        failed_phase = PHASE
    finally:
        phase("reverse-isolated-stop")
        stop_ok = True
        for component, instance in reversed(live):
            try:
                helper.cli(component, instance, "stop")
                require_disabled(helper, component, instance)
            except Exception:  # noqa: BLE001 - stop other fixtures even if one is unverified
                stop_ok = False
        try:
            helper.three_task_states(preflight)
        except Exception:  # noqa: BLE001
            stop_ok = False
        all_stopped = stop_ok
    print("I3_RESTART_FIXTURE_STOP=" + ("VERIFIED" if all_stopped else "NOT_VERIFIED"))
    require(all_stopped, "I3 fixture stop unverified; do not kill any arbitrary process")
    phase("incumbent-postflight")
    require(
        helper.production_snapshot(preflight) == production_tasks
        and same_generation(production_before, _current_identity(incumbent)),
        "production Scheduler or Core process generation changed",
    )
    print("I3_RESTART_PRODUCTION_TASKS_AND_CORE=UNCHANGED")
    if failed is not None:
        phase(failed_phase)
        raise failed
    print("I3_NATIVE_INDEPENDENT_LIFECYCLES=PASS")


if __name__ == "__main__":
    try:
        native_main()
    except BaseException as exc:  # noqa: BLE001 - fixed redacted diagnostics
        print(
            "I3_RESTART_SMOKE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

"""Native Windows I2 Bridge-v2 live smoke: one isolated Task Scheduler fixture.

Run ONLY after a successful one-use I2 install that registered the private
i2-smoke Bridge Disabled. This script starts/stops only that exact task.
All signed health requests use the Bridge's operator-private fixture grant.
No provider, Repo Ops, production credentials, public tunnel or Core is touched.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

INSTANCE = "i2-smoke"
TASK = "ServerFS v25 i2-smoke Agent Bridge"
PHASE = "entry"


class I2SmokeError(RuntimeError):
    """Redacted I2 native acceptance refusal."""


def require(value: bool, reason: str) -> None:
    if not value:
        raise I2SmokeError(reason)


def mark(name: str) -> None:
    global PHASE
    PHASE = name


def _preflight_module():
    import importlib.util

    script = Path(__file__).with_name("v0250-i2-native-preflight.py")
    spec = importlib.util.spec_from_file_location("serverfs_i2_smoke_gate", script)
    require(spec is not None and spec.loader is not None, "I2 inventory source is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(module.INSTANCE == INSTANCE and module.TASK == TASK, "I2 fixture identity mismatches")
    return module


def production_snapshot(gate) -> tuple[tuple[str, str, bool], ...]:
    rows = gate._scheduler_inventory()
    require(isinstance(rows, list), "I2 native Scheduler inventory is malformed")
    only_production = [row for row in rows if row.get("name") in gate.PRODUCTION]
    gate.verify_scheduler_rows(only_production)
    return tuple(sorted((item["name"], item["state"], item["enabled"]) for item in only_production))


def cli(action: str) -> dict:
    require(action in ("status", "start", "stop"), "I2 lifecycle action is not allowlisted")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "serverfs_mcp.windows_v25.cli",
            "bridge",
            action,
            "--instance",
            INSTANCE,
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=35,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        print("I2_CLI_" + action.upper() + "=FAILED", file=sys.stderr)
        raise I2SmokeError("I2 scoped Bridge lifecycle operation was refused")
    try:
        parsed = json.loads(result.stdout.decode("utf-8"))
        require(isinstance(parsed, dict), "I2 Bridge CLI response is not an object")
        return parsed
    except (UnicodeError, ValueError, TypeError) as exc:
        raise I2SmokeError("I2 Bridge CLI returned malformed evidence") from exc


def fixture_state(gate) -> tuple[str, bool]:
    rows = gate._scheduler_inventory()
    require(isinstance(rows, list), "I2 fixture state is inaccessible")
    matches = [row for row in rows if row.get("name") == TASK]
    require(len(matches) == 1, "I2 Bridge Task Scheduler identity is absent or ambiguous")
    state, enabled = matches[0].get("state"), matches[0].get("enabled")
    require(isinstance(state, str) and type(enabled) is bool, "I2 fixture task state invalid")
    return state, enabled


async def signed_health(plan, sid: str) -> None:
    from serverfs_mcp.agent_client import AgentBridgeRemoteError
    from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient
    from serverfs_mcp.windows_v25.bridge_peer_job import make_bridge_peer_validator

    validator = make_bridge_peer_validator(bridge_id=INSTANCE, user_sid=sid)
    settings = {
        "socket_path": Path(plan.pipe_name),
        "core_id": plan.health_core_id,
        "bridge_id": plan.bridge_id,
        "key_id": plan.health_key_id,
        "policy_revisions": {plan.health_workdir: plan.health_policy_digest},
        "server_pid_validator": validator,
        "timeout_seconds": 5.0,
    }
    client = CoreGrantBridgeClient(secret=plan.health_secret, **settings)
    mark("signed-bridge-ping")
    response = await client.call("bridge.ping", {"workdir": plan.health_workdir})
    require(response == {"status": "ok"}, "I2 private signed Bridge health was not accepted")
    print("I2_SIGNED_PIPE_AND_KERNEL_JOB=PASS")
    mark("wrong-hmac-denial")
    fake_secret = bytes(byte ^ 0xFF for byte in plan.health_secret)
    other = CoreGrantBridgeClient(secret=fake_secret, **settings)
    try:
        await other.call("bridge.ping", {"workdir": plan.health_workdir})
    except AgentBridgeRemoteError as exc:
        require(exc.code == "CORE_NOT_AUTHORIZED", "I2 bad HMAC returned an unknown error")
    else:
        raise I2SmokeError("I2 Bridge unexpectedly accepted the wrong Core signature")
    print("I2_UNAUTHORIZED_HMAC=DENIED")


def main() -> None:
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.readiness import _current_identity

    mark("native-interpreter")
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "non-elevated native Windows private Python -I -B required",
    )
    expected = storage.root(INSTANCE) / "envs" / "bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I2 smoke is not using its private owner interpreter",
    )
    gate = _preflight_module()
    mark("production-task-preflight")
    baseline = production_snapshot(gate)
    production_core = storage.load("core")
    mark("production-core-identity")
    before_core = _current_identity(production_core)
    require(before_core is not None, "production Core process generation unavailable")
    mark("fixture-disabled-preflight")
    require(fixture_state(gate) == ("Disabled", False), "I2 fixture must begin Disabled")
    initial = cli("status")
    require(
        initial.get("task_name") == TASK
        and initial.get("registered") is True
        and not initial.get("enabled")
        and not initial.get("running"),
        "I2 Bridge fixture is not registered Disabled",
    )
    mark("bridge-private-owner")
    manifest = storage.load("bridge", INSTANCE)
    plan = load_native_bridge_plan(manifest)
    started = False
    failure: BaseException | None = None
    failed_phase = ""
    stopped = False
    try:
        started = True
        mark("fixture-start")
        started_state = cli("start")
        require(
            started_state.get("task_name") == TASK
            and started_state.get("running") is True
            and started_state.get("readiness") == "owner_alive",
            "I2 independently owned Bridge process is not verified",
        )
        mark("signed-bridge-health")
        asyncio.run(asyncio.wait_for(signed_health(plan, manifest.sid), timeout=16))
    except BaseException as exc:
        failed_phase = PHASE
        failure = exc
    finally:
        if started:
            try:
                mark("fixture-stop")
                stopped_state = cli("stop")
                stopped = (
                    stopped_state.get("state") == "Disabled"
                    and not stopped_state.get("enabled")
                    and not stopped_state.get("running")
                    and cli("status").get("readiness") == "offline"
                    and fixture_state(gate) == ("Disabled", False)
                )
            except Exception:
                stopped = False
    print("I2_FIXTURE_STOP=" + ("VERIFIED" if stopped else "NOT_VERIFIED"))
    require(stopped, "I2 fixture stop unverified; do not kill an arbitrary PID")
    mark("production-postflight")
    require(production_snapshot(gate) == baseline, "production Scheduler state changed")
    after_core = _current_identity(production_core)
    require(
        after_core is not None
        and after_core.pid == before_core.pid
        and after_core.creation_time == before_core.creation_time,
        "production Core process generation changed",
    )
    print("I2_PRODUCTION_TASKS_AND_CORE=UNCHANGED")
    if failure is not None:
        mark(failed_phase)
        raise failure
    print("I2_NATIVE_BRIDGE_LIFECYCLE=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - redacted failure categories only
        print(
            "I2_NATIVE_SMOKE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

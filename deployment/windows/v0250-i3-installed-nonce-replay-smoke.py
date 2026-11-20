"""I3 installed Bridge v2: prove persisted nonce replay denial across restart.

Only the exact i3-shared Bridge owner may start/stop. Both I3 Cores stay
Disabled and no grant, credential or policy file is written. A single
HMAC-signed ping envelope is sent before and after a real Task Scheduler
Bridge restart, with its timestamp still demonstrably inside the allowed
clock-skew window; fresh Core A and B requests must still succeed.

Run once with i3-shared private owner Python -I -B, never an arbitrary venv.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import sys
import time
from pathlib import Path

BRIDGE = "i3-shared"
CORE_IDS = ("i3-core-a", "i3-core-b")
PHASE = "entry"


class I3ReplayError(RuntimeError):
    """Only sanitized isolated fixture refusal, never HMAC material."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise I3ReplayError(reason)


def helper():
    path = Path(__file__).with_name("v0250-i3-readonly-live-smoke.py")
    spec = importlib.util.spec_from_file_location("i3_nonce_scoped_helper", path)
    require(spec is not None and spec.loader is not None, "I3 helper unavailable")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    require(
        result.BRIDGE == BRIDGE and tuple(result.CORE_PORTS) == CORE_IDS,
        "I3 fixed instance scope mismatches existing fixture",
    )
    return result


def signed_ping(binding, *, issued_at: int) -> tuple[bytes, str]:
    """One exact envelope, with no signature/nonce/body in logs or exceptions."""
    request_id = "mcp_" + secrets.token_hex(12)
    unsigned = {
        "protocol_version": 2,
        "bridge_id": binding.bridge_id,
        "core_id": binding.instance_id,
        "key_id": binding.key_id,
        "request_id": request_id,
        "method": "bridge.ping",
        "params": {"workdir": "Fixture"},
        "policy_revision": dict(binding.policy_revisions)["Fixture"],
        "issued_at": issued_at,
        "nonce": secrets.token_hex(16),
    }
    canonical = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    signature = hmac.new(binding.secret, canonical, hashlib.sha256).hexdigest()
    encoded = (
        json.dumps(
            {**unsigned, "signature": signature},
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    require(len(encoded) < 4096, "I3 HMAC ping request exceeds fixed bound")
    return encoded, request_id


async def exchange(client, message: bytes, request_id: str) -> dict:
    return await asyncio.wait_for(client._exchange(message, request_id), timeout=18)


async def denied_replay(client, message: bytes, request_id: str) -> None:
    from serverfs_mcp.agent_client import AgentBridgeRemoteError

    try:
        await exchange(client, message, request_id)
    except AgentBridgeRemoteError as exc:
        require(exc.code == "CORE_NOT_AUTHORIZED", "I3 replay denial code differs")
    else:
        raise I3ReplayError("I3 replay was accepted")


def main() -> None:
    global PHASE

    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_peer_job import make_bridge_peer_validator
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity

    PHASE = "native-private-owner"
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I3 replay smoke requires native non-admin Python -I -B",
    )
    expected = storage.root(BRIDGE) / "envs" / "bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I3 replay smoke must run under the installed private Bridge owner",
    )

    probe = helper()
    gate = probe.gate()
    PHASE = "production-and-fixture-preflight"
    incumbent = storage.load("core")
    before_process = _current_identity(incumbent)
    require(before_process is not None, "production Core generation not observed")
    prod_tasks = probe.production_snapshot(gate)
    probe.three_task_states(gate)
    bridge = storage.load("bridge", BRIDGE)
    load_native_bridge_plan(bridge)

    bindings = {}
    for core_id in CORE_IDS:
        manifest = storage.load("core", core_id)
        workdirs, settings = load_native_config(Path(str(manifest.settings["config"])))
        require(settings.bridge_required, "I3 Core Bridge configuration has drifted")
        bindings[core_id] = load_native_core_bridge_binding(manifest, workdirs)
        require(
            set(dict(bindings[core_id].policy_revisions)) == {"Fixture"},
            "I3 Core grant no longer matches isolated fixture",
        )
    require(
        bindings[CORE_IDS[0]].secret != bindings[CORE_IDS[1]].secret,
        "I3 Core secrets must be independent",
    )
    peer = make_bridge_peer_validator(bridge_id=BRIDGE, user_sid=storage._sid())
    clients = {
        core_id: binding.build_wiring(peer_validator=peer)[1]
        for core_id, binding in bindings.items()
    }

    started = False
    failure: BaseException | None = None
    failed_phase = ""
    stopped = False
    try:
        PHASE = "bridge-initial-start"
        started = True
        initial = probe.cli("bridge", BRIDGE, "start")
        require(
            initial.get("running") is True and initial.get("readiness") == "owner_alive",
            "I3 Bridge initial owner not ready",
        )
        PHASE = "initial-fresh-authorizations"
        asyncio.run(asyncio.wait_for(probe.shared_bridge_rpc(bindings), timeout=26))
        issued = int(time.time())
        raw, request_id = signed_ping(bindings[CORE_IDS[0]], issued_at=issued)
        first = asyncio.run(exchange(clients[CORE_IDS[0]], raw, request_id))
        require(first == {"status": "ok"}, "initial signed ping did not succeed")
        asyncio.run(denied_replay(clients[CORE_IDS[0]], raw, request_id))
        print("I3_INSTALLED_NONCE_REPLAY_SAME_GENERATION=DENIED")

        PHASE = "bridge-exact-stop"
        probe.cli("bridge", BRIDGE, "stop")
        probe.three_task_states(gate)
        PHASE = "bridge-exact-restart"
        again = probe.cli("bridge", BRIDGE, "start")
        require(
            again.get("running") is True and again.get("readiness") == "owner_alive",
            "I3 Bridge restarted owner not ready",
        )
        # Guard against false-positive nonce denial caused by clock expiry.
        require(
            0 <= int(time.time()) - issued <= 30,
            "I3 signed request would be too old for a meaningful replay proof",
        )
        PHASE = "durable-nonce-after-restart"
        asyncio.run(denied_replay(clients[CORE_IDS[0]], raw, request_id))
        print("I3_INSTALLED_NONCE_REPLAY_AFTER_RESTART=DENIED")
        PHASE = "fresh-authorizations-after-restart"
        asyncio.run(asyncio.wait_for(probe.shared_bridge_rpc(bindings), timeout=26))
        print("I3_INSTALLED_FRESH_TWO_CORE_GRANTS_AFTER_RESTART=PASS")
    except BaseException as exc:
        failure = exc
        failed_phase = PHASE
    finally:
        PHASE = "bridge-exact-final-stop"
        try:
            if started:
                response = probe.cli("bridge", BRIDGE, "stop")
                require(
                    response.get("state") == "Disabled"
                    and response.get("running") is False
                    and response.get("enabled") is False,
                    "I3 Bridge failed to stop cleanly",
                )
            probe.three_task_states(gate)
            stopped = True
        except Exception:
            stopped = False
    print("I3_INSTALLED_REPLAY_FIXTURE_STOP=" + ("VERIFIED" if stopped else "NOT_VERIFIED"))
    require(stopped, "I3 scoped stop unverified; no arbitrary process kill")
    PHASE = "incumbent-postflight"
    require(
        probe.production_snapshot(gate) == prod_tasks,
        "production Scheduler baseline changed",
    )
    after = _current_identity(incumbent)
    require(
        after is not None
        and after.pid == before_process.pid
        and after.creation_time == before_process.creation_time,
        "production Core generation changed",
    )
    print("I3_INSTALLED_REPLAY_PRODUCTION_TASKS_AND_CORE=UNCHANGED")
    if failure is not None:
        PHASE = failed_phase
        raise failure
    print("I3_INSTALLED_DURABLE_NONCE=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - redacted fixed phase/type only
        print(
            "I3_INSTALLED_NONCE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

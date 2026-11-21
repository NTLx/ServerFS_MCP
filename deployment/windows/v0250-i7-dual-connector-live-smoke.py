"""I7 installed Windows dual real Cloudflare Connector lifecycle canary.

Uses exactly the prepared I7 Core and two independently registered Connector
owners. No DNS, Cloudflare control-plane mutation, production lifecycle, token
printing, or additional Scheduler registration. Always attempts scoped cleanup.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

INSTANCE = "i7-public"
CONNECTORS = ("i7-edge-a", "i7-edge-b")
PHASE = "entry"


class I7DualRefusal(RuntimeError):
    """Sanitized lifecycle failure."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I7DualRefusal(message)


def gate():
    file = Path(__file__).with_name("v0250-i7-connectors-disabled-register.py")
    spec = importlib.util.spec_from_file_location("i7_dual_registered_gate", file)
    require(spec is not None and spec.loader is not None, "installed gate unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli(component: str, action: str, connector: str | None = None) -> dict:
    from serverfs_mcp.windows_v25 import storage

    require(component in ("core", "cloudflare"), "invalid component")
    require(action in ("start", "stop", "status"), "invalid action")
    require(component != "core" or connector is None, "invalid Core connector")
    require(component != "cloudflare" or connector in CONNECTORS, "invalid Connector ID")
    root = storage.root(INSTANCE)
    executable = root / "envs" / component / "Scripts/python.exe"
    storage.check_private(executable, directory=False, sid=storage._sid())
    command = [
        str(executable),
        "-I",
        "-B",
        "-m",
        "serverfs_mcp.windows_v25.cli",
        component,
        action,
        "--instance",
        INSTANCE,
    ]
    if connector is not None:
        command.extend(("--connector-id", connector))
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=75,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0, "scoped CLI " + component + "/" + action + " refused")
    try:
        record = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise I7DualRefusal("invalid CLI status") from exc
    require(isinstance(record, dict), "CLI returned non-record")
    return record


def active(component: str, connector: str | None = None) -> bool:
    state = cli(component, "status", connector)
    expected = "verified" if component == "core" else "owner_alive"
    return state.get("running") is True and state.get("readiness") == expected


def offline(component: str, connector: str | None = None) -> bool:
    state = cli(component, "status", connector)
    return (
        state.get("registered") is True
        and state.get("running") is False
        and state.get("enabled") is False
        and state.get("state") == "Disabled"
    )


def main() -> None:
    global PHASE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native non-elevated Windows -I -B required",
    )
    target = storage.root(INSTANCE) / "envs/core/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == target.resolve(strict=True)
        and current_user_sid() == storage._sid(),
        "private I7 Core Python and SID required",
    )
    PHASE = "initial-disabled-postflight"
    registered = gate()
    registered.postflight()
    prod = storage.load("core")
    prod_generation = _current_identity(prod)
    require(prod_generation is not None, "production Core identity unavailable")

    started: list[tuple[str, str | None]] = []
    failure: BaseException | None = None
    failed_phase = ""
    cleanup_ok = True
    try:
        PHASE = "core-start"
        started.append(("core", None))  # start may partially enable before failing
        require(
            active("core") or cli("core", "start").get("readiness") == "verified",
            "I7 Core not ready",
        )
        print("I7_DUAL_LIVE_CORE=VERIFIED")
        for connector in CONNECTORS:
            PHASE = "connector-start-" + connector
            started.append(("cloudflare", connector))
            require(
                cli("cloudflare", "start", connector).get("readiness") == "owner_alive",
                "I7 Connector owner not ready",
            )
            print("I7_DUAL_LIVE_" + connector.upper().replace("-", "_") + "=OWNER_ALIVE")
        PHASE = "two-independent-owners"
        time.sleep(8)
        from serverfs_mcp.windows_v25 import storage as s

        a = s.load("cloudflare", INSTANCE, CONNECTORS[0])
        b = s.load("cloudflare", INSTANCE, CONNECTORS[1])
        a_identity = _current_identity(a)
        b_identity = _current_identity(b)
        require(
            a_identity is not None
            and b_identity is not None
            and (a_identity.pid, a_identity.creation_time)
            != (b_identity.pid, b_identity.creation_time)
            and all(active("cloudflare", name) for name in CONNECTORS),
            "I7 distinct owners are not both alive",
        )
        print("I7_DUAL_LIVE_DISTINCT_CONNECTOR_OWNERS=PASS")
        PHASE = "independent-stop-a"
        require(
            offline("cloudflare", CONNECTORS[0])
            or cli("cloudflare", "stop", CONNECTORS[0]).get("state") == "Disabled",
            "I7 Connector A stop refused",
        )
        now_b = _current_identity(b)
        require(
            now_b is not None
            and now_b.pid == b_identity.pid
            and now_b.creation_time == b_identity.creation_time
            and active("cloudflare", CONNECTORS[1])
            and active("core"),
            "I7 Connector B/Core changed when A stopped",
        )
        print("I7_DUAL_LIVE_PEER_SURVIVES_A_STOP=PASS")
        PHASE = "independent-stop-b"
        require(
            cli("cloudflare", "stop", CONNECTORS[1]).get("state") == "Disabled",
            "I7 Connector B stop refused",
        )
        require(active("core"), "I7 Core changed when Connectors stopped")
        print("I7_DUAL_LIVE_CORE_SURVIVES_CONNECTOR_STOP=PASS")
    except BaseException as exc:  # noqa: BLE001
        failure, failed_phase = exc, PHASE
    finally:
        PHASE = "scope-only-finally-stop"
        for component, connector in reversed(started):
            try:
                if not offline(component, connector):
                    cli(component, "stop", connector)
                cleanup_ok = offline(component, connector) and cleanup_ok
            except BaseException:
                cleanup_ok = False

    print("I7_DUAL_LIVE_FINALLY_DISABLED=" + ("PASS" if cleanup_ok else "FAILED"))
    require(cleanup_ok, "scoped service cleanup unverified")
    PHASE = "final-disabled-postflight"
    registered.postflight()
    require(
        offline("core") and all(offline("cloudflare", x) for x in CONNECTORS),
        "I7 service offline contract changed",
    )
    current = _current_identity(prod)
    require(
        current is not None
        and current.pid == prod_generation.pid
        and current.creation_time == prod_generation.creation_time,
        "production Core process generation changed",
    )
    print("I7_DUAL_LIVE_PRODUCTION_GENERATION=UNCHANGED")
    if failure is not None:
        PHASE = failed_phase
        raise failure
    print("I7_DUAL_CLOUDFLARE_OWNER_NATIVE_LIFECYCLE=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no private paths or credential leaks
        print(
            "I7_DUAL_LIVE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

"""I13 isolated Windows Scheduler activation probe for only Bridge i13-a.

One-use diagnostic after readonly I13 installed/post-start checks, no
Provider/Core/Git/Connector. Always attempt exact Bridge stop, preserve
initial Scheduler failure stage and separately prove final Disabled/offline.
Do not print any private path, credentials or arbitrary exception message.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import sys
from pathlib import Path

INSTANCE = "i13-a"
PHASE = "entry"


class I13ActivationRefusal(RuntimeError):
    """No unredacted exception text reaches terminal."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I13ActivationRefusal(reason)


def helper(filename: str, label: str):
    spec = importlib.util.spec_from_file_location(label, Path(__file__).with_name(filename))
    require(spec is not None and spec.loader is not None, "approved helper missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify_start(exc: BaseException) -> str:
    if type(exc).__name__ == "TaskError":
        match = re.fullmatch(
            r"component start refused at (enable|run|ready); "
            r"rollback=(UNVERIFIED|disabled-and-ended)",
            str(exc),
        )
        if match:
            return "stage=" + match.group(1) + " rollback=" + match.group(2)
        return "stage=OTHER_REDACTED"
    return "type=" + type(exc).__name__


def main() -> None:
    global PHASE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import cli, storage
    from serverfs_mcp.windows_v25.legacy_bridge_ownership import (
        assert_legacy_writable_roots_disjoint,
    )
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(sys.argv[1:] == ["--run-once"], "exact one-use activation gate required")
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "Windows ordinary SID -I -B required",
    )
    expected_python = storage.root(INSTANCE) / "envs/bridge/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected_python.resolve(strict=True),
        "exact i13-a installed Bridge Python required",
    )
    installed = helper("v0250-i13-owned-repo-stage.py", "i13_activation_installed")
    old = helper("v0250-i4-independent-live-smoke.py", "i13_activation_protected")
    gate = helper("v0250-i4-independent-preflight.py", "i13_activation_gate")
    PHASE = "protected-native-offline-preflight"
    installed.postflight()
    baseline = old.protected_baseline(gate)
    prod = storage.load("core")
    generation = _current_identity(prod)
    require(generation is not None, "protected production Core unavailable")
    bridge = storage.load("bridge", INSTANCE)
    PHASE = "exact-bridge-readonly-authorization"
    cli._preflight(bridge)
    cli._assert_instance_isolation(bridge)
    assert_legacy_writable_roots_disjoint(bridge)
    scheduler = Scheduler()
    state = scheduler.status(bridge)
    require(
        state.get("registered") is True
        and state.get("enabled") is False
        and state.get("state") == "Disabled"
        and not state.get("running"),
        "I13-A Bridge not initially disabled",
    )

    started = False
    failure: BaseException | None = None
    cleanup_ok = False
    PHASE = "single-bridge-a-native-activation"
    try:
        state = scheduler.start(bridge)
        require(
            state.get("registered") is True
            and state.get("enabled") is True
            and state.get("running") is True
            and state.get("readiness") == "owner_alive"
            and _current_identity(bridge) is not None,
            "Scheduler reported inconsistent Bridge owner readiness",
        )
        started = True
        print("I13_BRIDGE_A_SINGLE_NATIVE_START=PASS")
    except BaseException as exc:  # noqa: BLE001
        failure = exc
        print("I13_BRIDGE_A_SINGLE_NATIVE_START=FAILED " + classify_start(exc))
    finally:
        PHASE = "exact-bridge-a-stop-and-disabled-proof"
        try:
            state = scheduler.stop(bridge)
            cleanup_ok = (
                state.get("registered") is True
                and state.get("enabled") is False
                and state.get("state") == "Disabled"
                and not state.get("running")
                and _current_identity(bridge) is None
            )
        except BaseException:
            cleanup_ok = False
        print("I13_BRIDGE_A_EXACT_DISABLED_CLEANUP=" + ("PASS" if cleanup_ok else "UNVERIFIED"))
    require(cleanup_ok, "single Bridge stop unverified")
    PHASE = "final-protected-installed-acceptance"
    installed.postflight()
    require(
        old.protected_baseline(gate) == baseline
        and old.same_generation(generation, _current_identity(prod)),
        "protected production generation or task inventory changed",
    )
    print("I13_BRIDGE_A_PROTECTED_PRODUCTION_AND_I4_I11=UNCHANGED")
    if failure is not None:
        raise I13ActivationRefusal("native activation failed") from None
    require(started, "native Bridge start was not observed")
    print("I13_BRIDGE_A_SINGLE_OWNER_ACTIVATION_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001
        print(
            "I13_BRIDGE_A_ACTIVATION=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

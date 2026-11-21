"""Read-only native evidence after I5 Bridge startup refusal.

Never starts/stops a task, reads credentials, or echoes raw runtime logs.
Uses only exact I5 private owner state, confirmed protected Scheduler baseline,
and an allowlisted exception-class marker from the protected runtime.log.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import sys
from pathlib import Path

_FAILED = re.compile(r"windows-v25: bridge failed \(([A-Za-z][A-Za-z0-9_]{0,99})\)")


class I5PostfailureError(RuntimeError):
    """Do not leak task XML, credentials, protected log contents, or paths."""


def require(value: bool, reason: str) -> None:
    if not value:
        raise I5PostfailureError(reason)


def failure_types(text: str) -> tuple[str, ...]:
    """Only sanitized Python class names, never log contents."""
    return tuple(_FAILED.findall(text))[-5:]


def helper():
    source = Path(__file__).with_name("v0250-i5-provider-shared-preflight.py")
    spec = importlib.util.spec_from_file_location("i5_postfailure_gate", source)
    require(spec is not None and spec.loader is not None, "I5 host gate unavailable")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def main() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.legacy_bridge_ownership import (
        assert_legacy_writable_roots_disjoint,
    )
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I5 postfailure requires non-elevated Windows Python -I -B",
    )
    gate = helper()
    gate.protected_tasks()
    gate.verify_candidates(gate.candidates(), installed=True)
    print("I5_BRIDGE_POSTFAIL_PROTECTED_AND_THREE_DISABLED=PASS")

    manifest = storage.load("bridge", "i5-shared")
    require(
        manifest.component == "bridge" and manifest.instance_id == "i5-shared",
        "I5 installed Bridge manifest identity differs",
    )
    status = Scheduler().status(manifest)
    require(
        status.get("registered") is True
        and status.get("state") == "Disabled"
        and status.get("enabled") is False
        and status.get("running") is False
        and status.get("readiness") == "offline"
        and _current_identity(manifest) is None,
        "I5 Bridge not completely offline",
    )
    print("I5_BRIDGE_POSTFAIL_OWNER_OFFLINE=VERIFIED")

    try:
        load_native_bridge_plan(manifest)
    except Exception as exc:
        print("I5_BRIDGE_POSTFAIL_PRIVATE_PLAN=FAILED type=" + type(exc).__name__)
    else:
        print("I5_BRIDGE_POSTFAIL_PRIVATE_PLAN=PASS")
    try:
        assert_legacy_writable_roots_disjoint(manifest)
    except Exception as exc:
        print("I5_BRIDGE_POSTFAIL_LEGACY_OWNERSHIP=FAILED type=" + type(exc).__name__)
    else:
        print("I5_BRIDGE_POSTFAIL_LEGACY_OWNERSHIP=PASS")

    log = storage.home("bridge", "i5-shared") / "runtime.log"
    if log.is_file() and not log.is_symlink():
        storage.check_private(log, directory=False, sid=current_user_sid())
        require(log.stat().st_size <= 8 * 1024 * 1024, "I5 runtime log exceeded safe bound")
        with log.open("rb") as source:
            blob = source.read(8 * 1024 * 1024)
        found = failure_types(blob.decode("utf-8", errors="replace"))
        print("I5_BRIDGE_POSTFAIL_LOG_CLASS_COUNT=" + str(len(found)))
        if found:
            print("I5_BRIDGE_POSTFAIL_LAST_EXCEPTION=" + found[-1])
    else:
        print("I5_BRIDGE_POSTFAIL_LOG=ABSENT")
    print("I5_BRIDGE_POSTFAIL_READONLY=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - fixed exception type only
        print("I5_BRIDGE_POSTFAIL_READONLY=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

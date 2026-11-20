"""Read-only I4 installed owned Core/Bridge postflight after native smoke.

No task start/stop/enable/install, no credential read, and no network request.
Refuses any unknown host state; reports only fixed nonsecret result markers.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
from pathlib import Path

IDS = ("i4-a", "i4-b")
PORTS = (18871, 18872)


class I4PostflightError(RuntimeError):
    """Sanitized I4 read-only owner verification refusal."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise I4PostflightError(message)


def main() -> None:
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I4 postflight needs native non-elevated Python -I -B",
    )
    source = Path(__file__).with_name("v0250-i4-independent-preflight.py")
    spec = importlib.util.spec_from_file_location("i4_postflight_gate", source)
    require(spec is not None and spec.loader is not None, "I4 fixed gate source absent")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    require(tuple(gate.BRIDGES) == IDS, "I4 owned identity differs")
    gate.verify_task_rows(gate.scheduler_rows(), require_candidates_absent=False)
    print("I4_POSTFLIGHT_PROTECTED_SCHEDULER=EXPECTED")

    for instance in IDS:
        for component in ("bridge", "core"):
            manifest = storage.load(component, instance)
            require(
                manifest.instance_id == instance and manifest.task_name() in gate.TARGETS,
                "I4 installed manifest identity differs",
            )
            status = Scheduler().status(manifest)
            require(
                status.get("registered") is True
                and status.get("state") == "Disabled"
                and status.get("enabled") is False
                and status.get("running") is False
                and status.get("readiness") == "offline",
                "I4 fixture Task Scheduler state is not Disabled/offline",
            )
            require(
                _current_identity(manifest) is None,
                "I4 fixture process generation remains resident",
            )
    print("I4_POSTFLIGHT_FOUR_TASKS_DISABLED_OFFLINE=PASS")

    current_listeners = _windows_ipv4_tcp_listeners()
    require(
        not any(listener.port in PORTS for listener in current_listeners),
        "I4 fixture loopback listener remains allocated",
    )
    print("I4_POSTFLIGHT_PORTS_18871_18872_UNBOUND=PASS")
    print("I4_POSTFLIGHT_READONLY=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no error values/paths in diagnostic
        print("I4_POSTFLIGHT=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

"""Read-only Windows v0.25 installed Scheduler-code parity release gate.

Checks only the source bytes of windows_v25/tasks.py in three known private
nonproduction interpreters. No task operation, package installation, token,
network call, provider or production mutation. Exit 2 if any target is stale,
missing, unsafe, or unreadable; never print filesystem or credential details.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

INSTANCE_TARGETS = (
    ("I7_CORE", "i7-public", "core"),
    ("I7_CLOUDFLARE", "i7-public", "cloudflare"),
    ("I5_BRIDGE_OWNER", "i5-shared", "bridge"),
)
RELATIVE_MODULE = Path("Lib/site-packages/serverfs_mcp/windows_v25/tasks.py")
REPO = Path(__file__).resolve().parents[2]


class InstalledParityError(RuntimeError):
    """Sanitized, source-only acceptance failure."""


def classify_source(source: bytes, installed: Path) -> str:
    """Pure byte-parity classification; caller enforces private Windows ACL."""
    if installed.is_symlink():
        return "UNSAFE"
    if not installed.is_file():
        return "ABSENT"
    return "MATCH" if installed.read_bytes() == source else "STALE"


def main() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    if not (
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid()
    ):
        raise InstalledParityError("native non-elevated Windows -I -B owner required")
    source_path = REPO / "src/serverfs_mcp/windows_v25/tasks.py"
    if source_path.is_symlink() or not source_path.is_file():
        raise InstalledParityError("verified source file unavailable")
    source = source_path.read_bytes()
    if not source:
        raise InstalledParityError("empty Scheduler source")

    outcomes: list[str] = []
    sid = storage._sid()
    for label, instance, component in INSTANCE_TARGETS:
        env = storage.root(instance) / "envs" / component
        target = env / RELATIVE_MODULE
        try:
            storage.check_private(env, directory=True, sid=sid)
            if target.is_file() and not target.is_symlink():
                storage.check_private(target, directory=False, sid=sid)
            state = classify_source(source, target)
        except Exception:  # noqa: BLE001 - private path/ACL never leave this gate
            state = "UNVERIFIED"
        outcomes.append(state)
        print(label + "_WINDOWS_TASKS_PARITY=" + state)
    accepted = all(value == "MATCH" for value in outcomes)
    print("WINDOWS_NATIVE_SCHEDULER_INSTALLED_PARITY=" + ("PASS" if accepted else "BLOCKED"))
    if not accepted:
        raise InstalledParityError("installed code parity not established")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no private path leakage
        print("WINDOWS_SCHEDULER_PARITY=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

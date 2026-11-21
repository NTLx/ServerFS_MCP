"""Read-only, bounded v25 Cloudflare owner quiescence verification.

Windows Task Scheduler may transition to Disabled before its previous PythonW
generation exits. Never infer kernel quiescence merely from Task State.
No service/task/file mutations; only current-user verified identity checks.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STAGE = "entry"


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("Cloudflare component quiescence cannot be verified")


def verify_quiescent(
    manifest,
    *,
    scheduler,
    identity,
    pid_present,
    monotonic=time.monotonic,
    sleep=time.sleep,
    timeout_seconds: float = 12.0,
) -> bool:
    """Wait only for the trusted previous generation to be absent.

    The task must remain registered, Disabled, and not Running throughout.
    No PID or process is killed, and a stale live identity causes refusal.
    """
    deadline = monotonic() + timeout_seconds
    while True:
        state = scheduler.status(manifest)
        if not (
            state["registered"]
            and not state["enabled"]
            and not state["running"]
            and state["state"] == "Disabled"
        ):
            return False
        # Missing/stale readiness metadata alone is not proof of exit.
        # The PID from the protected previous generation must also be absent
        # from a fresh native process-table snapshot.
        if identity(manifest) is None and not pid_present():
            return True
        if monotonic() >= deadline:
            return False
        sleep(min(0.2, max(0.0, deadline - monotonic())))


def main() -> None:
    global STAGE
    from serverfs_mcp.windows_process import process_entries
    from serverfs_mcp.windows_v25 import readiness, storage
    from serverfs_mcp.windows_v25.tasks import Scheduler

    STAGE = "interpreter"
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
    )
    root = storage.root()
    expected = root / "envs" / "cloudflare"
    require(
        os.path.normcase(str(Path(sys.prefix).resolve(strict=True)))
        == os.path.normcase(str(expected.resolve(strict=True)))
    )

    STAGE = "private-registered-component"
    manifest = storage.load("cloudflare")
    scheduler = Scheduler()

    STAGE = "protected-previous-process-generation"
    path = readiness.identity_path("cloudflare")
    storage.check_private(path, directory=False, sid=manifest.sid)
    saved = json.loads(path.read_text(encoding="utf-8"))
    require(
        isinstance(saved, dict)
        and saved.get("schema") == 1
        and saved.get("component") == "cloudflare"
        and type(saved.get("pid")) is int
        and saved["pid"] > 0
        and type(saved.get("created")) is int
        and saved["created"] > 0
    )
    previous_pid = saved["pid"]

    STAGE = "bounded-kernel-quiescence"
    require(
        verify_quiescent(
            manifest,
            scheduler=scheduler,
            identity=readiness._current_identity,
            pid_present=lambda: any(entry.pid == previous_pid for entry in process_entries()),
        )
    )
    STAGE = "accepted"
    print("V25_CF_WINDOWLESS_OLD_GENERATION_QUIESCENT=PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V25_CF_WINDOWLESS_QUIESCENCE=FAILED:" + type(exc).__name__ + " stage=" + STAGE,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

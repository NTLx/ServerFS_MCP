"""Read-only proof that only the new Core owner is safely offline.

Run with the currently installed, dedicated core-owner-env Python -I -B.
Prints no private paths or PIDs; refuses an ambiguous prior generation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    if sys.platform != "win32" or os.name != "nt":
        print("W2J_CORE_GENERATION=FAILED (WindowsRequired)", file=sys.stderr)
        return 2
    try:
        from serverfs_mcp.native_lifecycle import window_local_app_data
        from serverfs_mcp.windows_core_control import (
            _require_previous_generation_stopped,
            status,
        )

        expected = window_local_app_data() / "ServerFS" / "core-owner-env"
        if Path(sys.prefix).resolve(strict=True) != expected.resolve(strict=True):
            raise ValueError("dedicated Core runtime identity differs")
        data = status()
        if not (
            data.get("registered") is True
            and data.get("task_valid") is True
            and data.get("manifest_installed") is True
            and data.get("enabled") is False
            and data.get("core_alive") is False
        ):
            raise ValueError("new Core is not verified registered/disabled/offline")
        _require_previous_generation_stopped()
    except Exception as exc:  # noqa: BLE001 - never disclose PID/path/credential
        print(f"W2J_CORE_GENERATION=FAILED ({type(exc).__name__})", file=sys.stderr)
        return 2
    print("W2J_CORE_GENERATION=PASS (new Core disabled and past generation gone)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

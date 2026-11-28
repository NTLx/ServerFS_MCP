"""Verify private Core venv metadata and CPython base-image binding, read-only.

Used only AFTER installing the byte-verified W2n Product wheel into the
disabled Core owner. No service, Task Scheduler, agent or network activity.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    if sys.platform != "win32" or os.name != "nt":
        print("W2N_CORE_VENV_HOME=FAILED (WindowsRequired)", file=sys.stderr)
        return 2
    try:
        if sys.argv[1:] == ["--source-preflight"]:
            # The current Core venv still contains W2j; inspect the new
            # W2n source helper without mutating that installed environment.
            repo = Path(__file__).resolve().parents[2]
            sys.path.insert(0, str(repo / "src"))
        elif len(sys.argv) != 1:
            raise ValueError("unsupported preflight mode")
        from serverfs_mcp.native_lifecycle import window_local_app_data
        from serverfs_mcp.windows_process import _venv_home_pythonw

        prefix = window_local_app_data() / "ServerFS" / "core-owner-env"
        python = prefix / "Scripts" / "python.exe"
        base_raw = getattr(sys, "_base_executable", None)
        if not isinstance(base_raw, str) or not base_raw:
            raise ValueError("CPython base executable path is unavailable")
        base = Path(base_raw).resolve(strict=True)
        if (
            Path(sys.prefix).resolve(strict=True) != prefix.resolve(strict=True)
            or Path(sys.executable).resolve(strict=True) != python.resolve(strict=True)
            or base.name.casefold() != "python.exe"
            or not sys.flags.isolated
            or not sys.dont_write_bytecode
        ):
            raise ValueError("private Core console interpreter identity differs")
        metadata_base = _venv_home_pythonw(prefix)
        if metadata_base is None or metadata_base != base.with_name("pythonw.exe").resolve(
            strict=True
        ):
            raise ValueError("Core pyvenv.cfg base PythonW disagrees with CPython runtime")
    except Exception as exc:  # noqa: BLE001 - redact user paths
        print(f"W2N_CORE_VENV_HOME=FAILED ({type(exc).__name__})", file=sys.stderr)
        return 2
    print("W2N_CORE_VENV_HOME=PASS (private Core venv and base PythonW agree)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

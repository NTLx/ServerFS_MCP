"""One-use Windows I13 Bridge A runner with durable sanitized terminal evidence.

This runner creates only the fixed output file under docs/diagnostics and
runs only the fixed installed i13-a Bridge activation script. A partial
STARTED-only capture is NOT success. It never runs Git, Provider, Connector,
Core, or production actions; target uses scoped Scheduler start/stop finally.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

EVIDENCE = "v0250-windows-i13-bridge-a-activation-capture-2026-11-17.txt"
TARGET = "v0250-i13-single-bridge-activation-smoke.py"
PREFIX = "I13_"


def safe_lines(content: str) -> tuple[str, ...]:
    """Only fixed bounded ASCII marker lines, no raw exceptions or paths."""
    return tuple(
        line
        for line in content.splitlines()
        if line.startswith(PREFIX)
        and len(line) <= 200
        and re.fullmatch(r"[A-Za-z0-9_ =-]+", line) is not None
    )


def main() -> int:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    if not (
        sys.argv[1:] == ["--run-once"]
        and os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid()
    ):
        return 2
    root = Path(__file__).resolve(strict=True).parents[2]
    script = Path(__file__).with_name(TARGET).resolve(strict=True)
    if not script.is_file() or script.parent != root / "deployment/windows":
        return 2
    python = storage.root("i13-a") / "envs/bridge/Scripts/python.exe"
    if not python.is_file() or python.is_symlink():
        return 2
    storage.check_private(python, directory=False, sid=storage._sid())
    output = root / "docs/diagnostics" / EVIDENCE
    with output.open("x", encoding="utf-8", newline="\n") as evidence:
        evidence.write("I13_ACTIVATION_CAPTURE=STARTED\n")
        evidence.flush()
        os.fsync(evidence.fileno())
        completed = subprocess.run(
            [str(python), "-I", "-B", str(script), "--run-once"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for stream in (completed.stdout, completed.stderr):
            for line in safe_lines(stream.decode("utf-8", errors="replace")):
                evidence.write(line + "\n")
                print(line, flush=True)
        evidence.write("I13_ACTIVATION_CHILD_EXIT=" + str(completed.returncode) + "\n")
        evidence.flush()
        os.fsync(evidence.fileno())
        print("I13_ACTIVATION_CHILD_EXIT=" + str(completed.returncode), flush=True)
        return completed.returncode


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:
        print("I13_ACTIVATION_RUNNER=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(exit_code)

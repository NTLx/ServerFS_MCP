"""Durable one-use I14 installed-Disabled postflight evidence, no service starts.

The I14 stage process may have lost its Bash background TaskOutput handle.
Never reconstruct a historical stage exit. Independently verify the resulting
installed Windows state with the original read-only I14 --postflight command.
Write only fixed bounded markers to a create-only source-controlled report.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

EVIDENCE = "v0250-windows-i14-disabled-postflight-capture-2026-11-17.txt"
TARGET = "v0250-i14-two-owned-repo-public-connector-stage.py"
REQUIRED = frozenset(
    {
        "I14_TWO_CONNECTORS_INDEPENDENT_OWNED_CORES=PASS",
        "I14_PRIVATE_DISABLED_CONNECTOR_POSTFLIGHT=PASS",
    }
)
PREFIXES = ("I14_", "I13_", "I7_")


def safe_lines(data: str) -> tuple[str, ...]:
    """No raw paths, secrets, OS errors or stdout from unrelated components."""
    return tuple(
        line
        for line in data.splitlines()
        if line.startswith(PREFIXES)
        and len(line) <= 155
        and re.fullmatch(r"[A-Z0-9_]+=(?:PASS|UNCHANGED|EXPECTED|VERIFIED)", line) is not None
    )


def main() -> int:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    if not (
        sys.argv[1:] == ["--capture-once"]
        and os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid()
    ):
        return 2
    root = Path(__file__).resolve(strict=True).parents[2]
    program = Path(__file__).with_name(TARGET).resolve(strict=True)
    expected = (root / ".venv/Scripts/python.exe").resolve(strict=True)
    if (
        program.parent != root / "deployment/windows"
        or not program.is_file()
        or Path(sys.executable).resolve(strict=True) != expected
    ):
        return 2
    output = root / "docs/diagnostics" / EVIDENCE
    with output.open("x", encoding="utf-8", newline="\n") as record:
        record.write("I14_DISABLED_POSTFLIGHT_CAPTURE=STARTED\n")
        record.flush()
        os.fsync(record.fileno())
        child = subprocess.run(
            [str(expected), "-I", "-u", "-B", str(program), "--postflight"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=240,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        markers = (
            *safe_lines(child.stdout.decode("utf-8", errors="replace")),
            *safe_lines(child.stderr.decode("utf-8", errors="replace")),
        )
        for line in markers:
            record.write(line + "\n")
            print(line, flush=True)
        record.write("I14_DISABLED_POSTFLIGHT_CHILD_EXIT=" + str(child.returncode) + "\n")
        good = child.returncode == 0 and REQUIRED <= set(markers)
        record.write("I14_DISABLED_POSTFLIGHT_EVIDENCE=" + ("PASS" if good else "FAILED") + "\n")
        record.flush()
        os.fsync(record.fileno())
        print("I14_DISABLED_POSTFLIGHT_CHILD_EXIT=" + str(child.returncode), flush=True)
        print("I14_DISABLED_POSTFLIGHT_EVIDENCE=" + ("PASS" if good else "FAILED"), flush=True)
        return 0 if good else 2


if __name__ == "__main__":
    try:
        status = main()
    except Exception as exc:
        print("I14_DISABLED_POSTFLIGHT_CAPTURE=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(status)

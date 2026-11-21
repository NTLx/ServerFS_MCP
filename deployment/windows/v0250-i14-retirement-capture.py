"""I14 exact two-Connector retirement + independent postflight durable record.

Required EXTERNAL precondition: both nonproduction Cloudflare routes restored
to original I7 origin/Host, independently read back. This runner has no
Cloudflare API, no production mutation and no Git operations. It invokes the
existing exact-allowlisted native I14 retirement ONCE, then independently
verifies installed state read-only. A STARTED-only record is never a PASS.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

EVIDENCE = "v0250-windows-i14-two-owned-public-retire-capture-2026-11-17.txt"
TARGET = "v0250-i14-two-owned-repo-public-connector-retire.py"
REQUIRED = frozenset(
    {
        "I14_TWO_NATIVE_TEMPORARY_CONNECTORS_DISABLED=VERIFIED",
        "I14_TWO_TEMPORARY_SCHEDULER_TASKS_UNREGISTERED=PASS",
        "I14_TWO_COPIED_TEST_ONLY_TOKENS_REMOVED=PASS",
        "I14_ONLY_SID_PRIVATE_RUNTIME_EVIDENCE_RETAINED=PASS",
        "I14_I13_I7_PRODUCTION_UNCHANGED=PASS",
        "I14_TEMPORARY_TWO_CONNECTORS_RETIRED=PASS",
    }
)
POST = frozenset(
    {
        "I14_ONLY_SID_PRIVATE_RUNTIME_EVIDENCE_RETAINED=PASS",
        "I14_I13_I7_PRODUCTION_UNCHANGED=PASS",
    }
)


def safe_lines(raw: str) -> tuple[str, ...]:
    """Allowlist bounded I14 status markers, never private data or path text."""
    return tuple(
        line
        for line in raw.splitlines()
        if line.startswith("I14_")
        and len(line) <= 160
        and re.fullmatch(r"[A-Z0-9_]+=(?:PASS|VERIFIED|UNCHANGED)", line) is not None
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
    script = Path(__file__).with_name(TARGET).resolve(strict=True)
    py = (root / ".venv/Scripts/python.exe").resolve(strict=True)
    if (
        script.parent != root / "deployment/windows"
        or not script.is_file()
        or Path(sys.executable).resolve(strict=True) != py
    ):
        return 2
    output = root / "docs/diagnostics" / EVIDENCE
    with output.open("x", encoding="utf-8", newline="\n") as evidence:
        evidence.write("I14_RETIRE_CAPTURE=STARTED\n")
        evidence.flush()
        os.fsync(evidence.fileno())
        command = [str(py), "-I", "-u", "-B", str(script)]
        retired = subprocess.run(
            [*command, "--retire-after-verified-remote-restore"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=240,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        results = (
            *safe_lines(retired.stdout.decode("utf-8", errors="replace")),
            *safe_lines(retired.stderr.decode("utf-8", errors="replace")),
        )
        for line in results:
            evidence.write(line + "\n")
            print(line, flush=True)
        evidence.write("I14_RETIRE_CHILD_EXIT=" + str(retired.returncode) + "\n")
        if retired.returncode != 0 or not REQUIRED <= set(results):
            evidence.write("I14_RETIRE_DURABLE_EVIDENCE=FAILED\n")
            evidence.flush()
            os.fsync(evidence.fileno())
            return 2
        checked = subprocess.run(
            [*command, "--postflight"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=240,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        markers = (
            *safe_lines(checked.stdout.decode("utf-8", errors="replace")),
            *safe_lines(checked.stderr.decode("utf-8", errors="replace")),
        )
        for line in markers:
            evidence.write(line + "\n")
            print(line, flush=True)
        evidence.write("I14_RETIRE_INDEPENDENT_POSTFLIGHT_EXIT=" + str(checked.returncode) + "\n")
        passed = checked.returncode == 0 and POST <= set(markers)
        evidence.write("I14_RETIRE_DURABLE_EVIDENCE=" + ("PASS" if passed else "FAILED") + "\n")
        evidence.flush()
        os.fsync(evidence.fileno())
        print("I14_RETIRE_CHILD_EXIT=" + str(retired.returncode), flush=True)
        print("I14_RETIRE_INDEPENDENT_POSTFLIGHT_EXIT=" + str(checked.returncode), flush=True)
        print("I14_RETIRE_DURABLE_EVIDENCE=" + ("PASS" if passed else "FAILED"), flush=True)
        return 0 if passed else 2


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:
        print("I14_RETIRE_RUNNER=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(code)

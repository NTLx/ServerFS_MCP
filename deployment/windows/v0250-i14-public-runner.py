"""One-use I14 dual-owned-Repo HTTPS E2E with durable bounded evidence.

Only after an operator independently confirms two NONPRODUCTION Cloudflare
Tunnel routes now target i13-a.local:18883 and i13-b.local:18884 with 404
fallback. This runner NEVER changes Tunnel configuration, DNS or credentials.
The target script starts/stops six exact disposable native owners in finally.
A STARTED-only record is NOT acceptance; never replay on partial results.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

TARGET = "v0250-i14-two-owned-repo-public-smoke.py"
EVIDENCE = "v0250-windows-i14-two-owned-repo-public-capture-2026-11-17.txt"
REQUIRED = frozenset(
    {
        "I14_SIX_INDEPENDENT_NATIVE_OWNER_TASKS=PASS",
        "I14_TWO_PUBLIC_REPO_MCP_AND_BEARER_ISOLATION=PASS",
        "I14_PUBLIC_B_REPO_SURVIVED_A_BRIDGE_CONNECTOR_STOP=PASS",
        "I14_SIX_NONPROD_OWNERS_DISABLED=PASS",
        "I14_PRODUCTION_I4_I11_AND_I7=UNCHANGED",
        "I14_TWO_INDEPENDENT_OWNED_REPO_PUBLIC_E2E=PASS",
    }
)


def safe_lines(data: str) -> tuple[str, ...]:
    """Only bounded I14 success/failure classification; no HTTP or Git payload."""
    return tuple(
        line
        for line in data.splitlines()
        if line.startswith("I14_")
        and len(line) <= 155
        and re.fullmatch(r"[A-Z0-9_]+=(?:PASS|DENIED|UNCHANGED|VERIFIED|UNVERIFIED)", line)
        is not None
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
    expected = storage.root("i13-a") / "envs/bridge/Scripts/python.exe"
    if (
        not script.is_file()
        or script.parent != root / "deployment/windows"
        or not expected.is_file()
        or expected.is_symlink()
    ):
        return 2
    storage.check_private(expected, directory=False, sid=storage._sid())
    report = root / "docs/diagnostics" / EVIDENCE
    with report.open("x", encoding="utf-8", newline="\n") as out:
        out.write("I14_PUBLIC_CAPTURE=STARTED\n")
        out.flush()
        os.fsync(out.fileno())
        completed = subprocess.run(
            [str(expected), "-I", "-u", "-B", str(script), "--run-once"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=600,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        markers = (
            *safe_lines(completed.stdout.decode("utf-8", errors="replace")),
            *safe_lines(completed.stderr.decode("utf-8", errors="replace")),
        )
        for line in markers:
            out.write(line + "\n")
            print(line, flush=True)
        out.write("I14_PUBLIC_CHILD_EXIT=" + str(completed.returncode) + "\n")
        passed = completed.returncode == 0 and REQUIRED <= set(markers)
        out.write("I14_PUBLIC_DURABLE_EVIDENCE=" + ("PASS" if passed else "FAILED") + "\n")
        out.flush()
        os.fsync(out.fileno())
        print("I14_PUBLIC_CHILD_EXIT=" + str(completed.returncode), flush=True)
        print("I14_PUBLIC_DURABLE_EVIDENCE=" + ("PASS" if passed else "FAILED"), flush=True)
        return 0 if passed else 2


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:
        print("I14_PUBLIC_RUNNER=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(code)

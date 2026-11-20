"""I10 exact one-use native Windows evidence runner for topology A.

Three independent, EXPLICIT modes: --stage-once, --public-once, --retire-once.
Every mode makes exactly one create-only durable capture and never invokes
another mode. HTTP route changes/restore are external independently audited
preconditions to public and retire; this code never uses Cloudflare API.
Retire is safe ONLY after both nonproduction routes return to original I7.
No secrets, paths or arbitrary child stdout may appear in durable evidence.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

MODES = {
    "stage": {
        "target": "v0250-i10-same-core-dual-connectors-stage.py",
        "args": ("--stage",),
        "env": "development",
        "capture": "v0250-windows-i10-disabled-stage-capture-2026-11-17.txt",
        "required": frozenset(
            {
                "I10_TWO_DISTINCT_PRIVATE_NONPROD_TOKENS=PASS",
                "I10_BOTH_CONNECTORS_SAME_CORE_DIFFERENT_TUNNELS=PASS",
                "I10_DISABLED_NATIVE_CONNECTOR_POSTFLIGHT=PASS",
                "I10_TWO_CONNECTORS_ONE_CORE_DISABLED_STAGE=PASS",
            }
        ),
        "post": "--postflight",
        "post_required": frozenset(
            {
                "I10_BOTH_CONNECTORS_SAME_CORE_DIFFERENT_TUNNELS=PASS",
                "I10_DISABLED_NATIVE_CONNECTOR_POSTFLIGHT=PASS",
            }
        ),
    },
    "public": {
        "target": "v0250-i10-same-core-public-bridge-smoke.py",
        "args": ("--run-once",),
        "env": "bridge",
        "capture": "v0250-windows-i10-public-single-core-capture-2026-11-17.txt",
        "required": frozenset(
            {
                "I10_SIGNED_ONE_CORE_SHARED_BRIDGE=PASS",
                "I10_TWO_INDEPENDENT_NATIVE_CONNECTOR_OWNERS=PASS",
                "I10_TWO_PUBLIC_HTTPS_FOREIGN_BEARERS_DENIED=PASS",
                "I10_TWO_PUBLIC_MCP_SESSIONS_SAME_CORE_BRIDGE=PASS",
                "I10_ROUTE_B_SURVIVES_CONNECTOR_A_STOP=PASS",
                "I10_FOUR_SCOPED_TASKS_DISABLED=PASS",
                "I10_I7_I5_CORE_B_PRODUCTION_UNCHANGED=PASS",
                "I10_TWO_PUBLIC_CONNECTORS_ONE_CORE_AND_BRIDGE_E2E=PASS",
            }
        ),
        "post": None,
        "post_required": frozenset(),
    },
    "retire": {
        "target": "v0250-i10-public-connector-retire.py",
        "args": ("--retire-after-remote-restore",),
        "env": "development",
        "capture": "v0250-windows-i10-retire-capture-2026-11-17.txt",
        "required": frozenset(
            {
                "I10_TWO_TEMPORARY_CONNECTOR_OWNERS_OFFLINE=VERIFIED",
                "I10_TWO_TEMPORARY_TASKS_UNREGISTERED=PASS",
                "I10_TWO_COPIED_NONPROD_TOKENS_RETIRED=PASS",
                "I10_ONLY_NATIVE_RUNTIME_EVIDENCE_RETAINED=PASS",
                "I10_I7_I5_PRODUCTION_PROTECTED_POST_RETIRE=PASS",
                "I10_TEMPORARY_CONNECTOR_RETIRE=PASS",
            }
        ),
        "post": "--postflight",
        "post_required": frozenset(
            {
                "I10_ONLY_NATIVE_RUNTIME_EVIDENCE_RETAINED=PASS",
                "I10_I7_I5_PRODUCTION_PROTECTED_POST_RETIRE=PASS",
            }
        ),
    },
}


def safe_lines(raw: str) -> tuple[str, ...]:
    """Strict marker grammar: discard all raw credential/OS/Repo output."""
    return tuple(
        line
        for line in raw.splitlines()
        if line.startswith("I10_")
        and len(line) <= 160
        and re.fullmatch(r"[A-Z0-9_]+=(?:PASS|VERIFIED|UNCHANGED)", line) is not None
    )


def run(child: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        child,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=650,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def main() -> int:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    parser = argparse.ArgumentParser()
    options = parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--stage-once", action="store_true")
    options.add_argument("--public-once", action="store_true")
    options.add_argument("--retire-once", action="store_true")
    args = parser.parse_args()
    if not (
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid()
    ):
        return 2
    mode = "stage" if args.stage_once else "public" if args.public_once else "retire"
    plan = MODES[mode]
    root = Path(__file__).resolve(strict=True).parents[2]
    script = Path(__file__).with_name(plan["target"]).resolve(strict=True)
    if not script.is_file() or script.parent != root / "deployment/windows":
        return 2
    python = (
        root / ".venv/Scripts/python.exe"
        if plan["env"] == "development"
        else storage.root("i5-shared") / "envs/bridge/Scripts/python.exe"
    )
    if not python.is_file() or python.is_symlink():
        return 2
    if plan["env"] == "bridge":
        storage.check_private(python, directory=False, sid=storage._sid())
    if Path(sys.executable).resolve(strict=True) != (root / ".venv/Scripts/python.exe").resolve(
        strict=True
    ):
        return 2
    report = root / "docs/diagnostics" / plan["capture"]
    with report.open("x", encoding="utf-8", newline="\n") as evidence:
        prefix = "I10_" + mode.upper()
        evidence.write(prefix + "_CAPTURE=STARTED\n")
        evidence.flush()
        os.fsync(evidence.fileno())
        target = [str(python), "-I", "-u", "-B", str(script)]
        first = run([*target, *plan["args"]], root)
        output = (
            *safe_lines(first.stdout.decode("utf-8", errors="replace")),
            *safe_lines(first.stderr.decode("utf-8", errors="replace")),
        )
        for marker in output:
            evidence.write(marker + "\n")
            print(marker, flush=True)
        evidence.write(prefix + "_CHILD_EXIT=" + str(first.returncode) + "\n")
        if first.returncode != 0 or not plan["required"] <= set(output):
            evidence.write(prefix + "_DURABLE_EVIDENCE=FAILED\n")
            evidence.flush()
            os.fsync(evidence.fileno())
            return 2
        if plan["post"]:
            follow = run([*target, plan["post"]], root)
            follow_output = (
                *safe_lines(follow.stdout.decode("utf-8", errors="replace")),
                *safe_lines(follow.stderr.decode("utf-8", errors="replace")),
            )
            for marker in follow_output:
                evidence.write(marker + "\n")
                print(marker, flush=True)
            evidence.write(prefix + "_INDEPENDENT_POSTFLIGHT_EXIT=" + str(follow.returncode) + "\n")
            if follow.returncode != 0 or not plan["post_required"] <= set(follow_output):
                evidence.write(prefix + "_DURABLE_EVIDENCE=FAILED\n")
                evidence.flush()
                os.fsync(evidence.fileno())
                return 2
        evidence.write(prefix + "_DURABLE_EVIDENCE=PASS\n")
        evidence.flush()
        os.fsync(evidence.fileno())
        print(prefix + "_CHILD_EXIT=" + str(first.returncode), flush=True)
        print(prefix + "_DURABLE_EVIDENCE=PASS", flush=True)
        return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:
        print("I10_DURABLE_RUNNER=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(code)

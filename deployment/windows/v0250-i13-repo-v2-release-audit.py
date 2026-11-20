"""Readonly I13 v2 release-evidence gate for Windows native dual Repo Ops.

Validates the exact captured one-use E2E result and independently runs the
installed private I13 postflight. Does not launch the smoke, start services,
modify Git, invoke Providers or contact Cloudflare.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import sys
from pathlib import Path

EVIDENCE = "v0250-windows-i13-two-owned-repo-v2-capture-2026-11-17.txt"
START = "I13_REPO_V2_CAPTURE=STARTED"
REQUIRED = (
    "I13_REPO_V2_BRIDGE_I13_A_START=PASS",
    "I13_REPO_V2_BRIDGE_I13_B_START=PASS",
    "I13_REPO_V2_CORE_I13_A_START=PASS",
    "I13_REPO_V2_CORE_I13_B_START=PASS",
    "I13_REPO_V2_TWO_AUTHENTICATED_FIXED_GIT_ACTIONS=PASS",
    "I13_REPO_V2_CROSS_BEARER_AND_UNKNOWN_ACTION=DENIED",
    "I13_REPO_V2_PEER_B_REPO_SURVIVED_A_BRIDGE_STOP=PASS",
    "I13_REPO_V2_CLEANUP_CORE_I13_A=PASS",
    "I13_REPO_V2_CLEANUP_CORE_I13_B=PASS",
    "I13_REPO_V2_CLEANUP_BRIDGE_I13_A=PASS",
    "I13_REPO_V2_CLEANUP_BRIDGE_I13_B=PASS",
    "I13_REPO_V2_ALL_ATTEMPTED_OWNERS_STOPPED=PASS",
    "I13_REPO_V2_PRODUCTION_AND_I4_I11=UNCHANGED",
    "I13_REPO_V2_TWO_INDEPENDENT_NATIVE_REPO_E2E=PASS",
    "I13_REPO_V2_CHILD_EXIT=0",
)
BASELINE = (
    "I13_TWO_PRIVATE_REPO_GUARDS_AND_OWNED_WORKERS=PASS",
    "I13_FOUR_DISABLED_REPO_TASK_OWNERS_AND_GRANTS=PASS",
    "I13_PRODUCTION_AND_I4_I11=UNCHANGED",
)


class I13EvidenceRefusal(ValueError):
    """Never echo untrusted evidence contents in failure diagnostics."""


def require(condition: bool, label: str) -> None:
    if not condition:
        raise I13EvidenceRefusal(label)


def validate(content: str) -> None:
    """Accept one complete, bounded capture; reject partial/conflicting logs."""
    require(0 < len(content) <= 16_384, "evidence size outside fixed bound")
    lines = content.splitlines()
    require(
        len(lines) == 1 + len(REQUIRED) + 2 * len(BASELINE)
        and lines[0] == START
        and lines[-1] == "I13_REPO_V2_CHILD_EXIT=0",
        "evidence framing incomplete",
    )
    require(
        all(re.fullmatch(r"I13_[A-Z0-9_]+=(?:PASS|DENIED|UNCHANGED|STARTED|0)", s) for s in lines),
        "unexpected evidence value",
    )
    for marker in REQUIRED:
        require(lines.count(marker) == 1, "required result absent or duplicated")
    for marker in BASELINE:
        require(lines.count(marker) == 2, "pre/postflight evidence absent or duplicated")


def main() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    require(sys.argv[1:] == ["--audit"], "exact readonly audit flag required")
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "ordinary Windows SID -I -B required",
    )
    root = Path(__file__).resolve(strict=True).parents[2]
    path = root / "docs/diagnostics" / EVIDENCE
    require(path.is_file() and not path.is_symlink(), "evidence missing or unsafe")
    require(path.stat().st_size <= 16_384, "evidence file too large")
    validate(path.read_text(encoding="utf-8"))
    print("I13_REPO_V2_DURABLE_CHILD_EVIDENCE=PASS")

    script = Path(__file__).with_name("v0250-i13-owned-repo-stage.py")
    spec = importlib.util.spec_from_file_location("i13_readonly_installed_postflight", script)
    require(spec is not None and spec.loader is not None, "trusted postflight missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.postflight()
    print("I13_REPO_V2_INDEPENDENT_INSTALLED_POSTFLIGHT=PASS")
    print("I13_REPO_V2_READONLY_RELEASE_EVIDENCE=PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "I13_REPO_V2_RELEASE_AUDIT=FAILED type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

"""Create-only offline Wheel for seven approved I5 Bridge Owner source deltas.

No installed-package modifications, Scheduler starts or provider calls.
Require the exact private old Wheel to reproduce the currently installed
I5 shared Bridge Owner; the new Wheel must have exactly the same member
inventory, with changes only in seven allowlisted Windows v25 modules and
Wheel RECORD. One-use candidate slot is never overwritten.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import zipfile
from pathlib import Path

SLOT = "bridge-owner-seven-module-wheel-1"
REPO = Path(__file__).resolve().parents[2]
PHASE = "entry"


class I5WheelStageError(RuntimeError):
    """Sanitized refusal with no private files, credentials or model output."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5WheelStageError(reason)


def helper(name: str, label: str):
    source = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "fixed helper missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def only_private_wheel(directory: Path, sid: str) -> Path:
    from serverfs_mcp.windows_v25 import storage

    storage.check_private(directory, directory=True, sid=sid)
    require(directory.is_dir() and not directory.is_symlink(), "Wheel slot not private")
    candidates = tuple(directory.glob("serverfs_mcp-*.whl"))
    require(len(candidates) == 1, "expected one private candidate Wheel")
    wheel = candidates[0]
    require(wheel.is_file() and not wheel.is_symlink(), "candidate Wheel unsafe")
    storage.check_private(wheel, directory=False, sid=sid)
    return wheel


def verify_archive_delta(previous: Path, candidate: Path, *, allowed: frozenset[str]) -> None:
    with zipfile.ZipFile(previous) as old, zipfile.ZipFile(candidate) as new:
        require(old.testzip() is None and new.testzip() is None, "Wheel corrupt")
        a, b = set(old.namelist()), set(new.namelist())
        require(a == b, "unapproved Wheel member change")
        require(
            {"serverfs_mcp/" + name for name in allowed} <= a,
            "new Wheel missing source delta module",
        )
        changed = {name for name in a if old.read(name) != new.read(name)}
        allowed_entries = {"serverfs_mcp/" + name for name in allowed} | {
            name for name in a if name.endswith(".dist-info/RECORD")
        }
        require(changed <= allowed_entries, "unexpected Wheel binary or metadata delta")
        require(
            changed & {"serverfs_mcp/" + name for name in allowed}
            == {"serverfs_mcp/" + name for name in allowed},
            "Wheel is missing one of the seven approved code fixes",
        )


def baseline():
    gate = helper("v0250-i5-owner-private-wheel-audit.py", "i5_owner_wheel_preflight")
    require(gate.REPO == REPO and gate.INSTANCE == "i5-shared", "I5 owner identity changed")
    base, sid, source, installed, old = gate.native_preflight()
    require(len(old) == 1, "old I5 exact rollback wheel is ambiguous")
    require(
        {name for name in source if source[name] != installed[name]} == gate.EXPECTED_DELTA,
        "I5 seven-module source changes drifted",
    )
    return gate, base, sid, source, old[0]


def candidate_postflight(gate, slot: Path, sid: str, modules: dict[str, bytes], old: Path) -> None:
    wheel = only_private_wheel(slot, sid)
    require(
        gate.wheel_modules(wheel, set(modules)) == modules,
        "new I5 Wheel differs from checked source modules",
    )
    verify_archive_delta(old, wheel, allowed=gate.EXPECTED_DELTA)
    gate.native_preflight()
    print("I5_OWNER_SEVEN_MODULE_WHEEL_SOURCE_AND_DIFF=VERIFIED")


def main() -> None:
    global PHASE
    p = argparse.ArgumentParser()
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--audit", action="store_true")
    m.add_argument("--stage", action="store_true")
    m.add_argument("--postflight", action="store_true")
    args = p.parse_args()

    PHASE = "protected-seven-module-and-rollback-wheel-preflight"
    gate, base, sid, modules, old = baseline()
    print("I5_OWNER_ORIGINAL_PRIVATE_ROLLBACK_WHEEL=VERIFIED")
    slot = base / "build" / SLOT
    if args.postflight:
        PHASE = "read-only-candidate-postflight"
        candidate_postflight(gate, slot, sid, modules, old)
        print("I5_OWNER_SEVEN_MODULE_WHEEL_POSTFLIGHT=PASS")
        return
    require(not slot.exists() and not slot.is_symlink(), "one-use Wheel slot occupied")
    if args.audit:
        print("I5_OWNER_SEVEN_MODULE_WHEEL_AUDIT=PASS")
        return

    builder = helper("v0250-i2-stage.py", "i5_owner_wheel_builder")
    require(builder.REPO == REPO, "offline builder checkout mismatch")
    PHASE = "create-only-private-candidate-Wheel"
    builder._new_dir(slot, sid)
    wheel = builder._wheel(
        REPO,
        slot,
        "serverfs_mcp-*.whl",
        tuple(sorted(gate.EXPECTED_DELTA)),
    )
    builder._protect_tree(slot, sid)
    PHASE = "offline-Wheel-delta-proof"
    require(wheel == only_private_wheel(slot, sid), "Wheel candidate identity mismatch")
    candidate_postflight(gate, slot, sid, modules, old)
    print("I5_OWNER_SEVEN_MODULE_NEW_WHEEL=PASS")
    print("I5_OWNER_NO_PRIVATE_PACKAGE_INSTALL=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no sensitive exception content
        print(
            "I5_OWNER_SEVEN_MODULE_WHEEL=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

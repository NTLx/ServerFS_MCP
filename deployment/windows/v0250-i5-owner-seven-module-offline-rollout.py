"""One-use offline upgrade for ONLY the stopped I5 shared Bridge Owner.

The pre-upgrade installed owner exactly matches one retained private Wheel.
The candidate Wheel matches current source and differs only in the approved
seven Windows v25 modules and RECORD. No Core, Claude Worker, production, DNS
or credential modification. A partial failure restores the exact old Wheel
and verifies byte parity or reports an unverified rollback, never success.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

INSTANCE = "i5-shared"
MARKER = "bridge-owner-seven-module-rollout-1"
PHASE = "entry"


class I5OwnerRolloutError(RuntimeError):
    """Sanitized lifecycle refusal, with no private paths or credentials."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5OwnerRolloutError(reason)


def helper(name: str, label: str):
    import importlib.util

    source = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "fixed helper missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_args(python: Path, wheel: Path) -> list[str]:
    return [
        "uv",
        "pip",
        "install",
        "--offline",
        "--no-deps",
        "--reinstall",
        "--python",
        str(python),
        str(wheel),
    ]


def current_modules(env: Path, modules: dict[str, bytes], sid: str) -> dict[str, bytes]:
    from serverfs_mcp.windows_v25 import storage

    pkg = env / "Lib/site-packages/serverfs_mcp"
    storage.check_private(env, directory=True, sid=sid)
    return helper(
        "v0250-i5-owner-private-wheel-audit.py", "i5_install_byte_reader"
    ).installed_modules(pkg, modules, sid)


def preflight():
    from serverfs_mcp.windows_v25 import storage

    gate = helper("v0250-i5-owner-private-wheel-audit.py", "i5_install_owner_audit")
    root, sid, source, installed, previous = gate.native_preflight()
    require(gate.INSTANCE == INSTANCE and len(previous) == 1, "I5 rollback baseline differs")
    candidate_gate = helper(
        "v0250-i5-owner-seven-module-wheel-stage.py", "i5_install_wheel_candidate"
    )
    require(candidate_gate.REPO == gate.REPO, "source checkout differs")
    wheel = candidate_gate.only_private_wheel(root / "build" / candidate_gate.SLOT, sid)
    candidate_gate.verify_archive_delta(previous[0], wheel, allowed=gate.EXPECTED_DELTA)
    require(
        gate.wheel_modules(wheel, set(source)) == source,
        "candidate Wheel not identical to checked source",
    )
    env = root / "envs/bridge"
    py = env / "Scripts/python.exe"
    storage.check_private(py, directory=False, sid=sid)
    require(py.is_file() and not py.is_symlink(), "Bridge interpreter unsafe")
    require(current_modules(env, source, sid) == installed, "owner changed during preflight")
    return gate, candidate_gate, root, env, sid, source, installed, previous[0], wheel, py


def postflight() -> None:
    from serverfs_mcp.windows_v25 import storage

    gate = helper("v0250-i5-owner-private-wheel-audit.py", "i5_post_owner_audit")
    candidate_gate = helper("v0250-i5-owner-seven-module-wheel-stage.py", "i5_post_wheel")
    root, sid = storage.root(INSTANCE), storage._sid()
    source = gate.source_modules()
    wheel = candidate_gate.only_private_wheel(root / "build" / candidate_gate.SLOT, sid)
    # The retained private old Wheel was verified by the pre-upgrade audit.
    expected = gate.wheel_modules(wheel, set(source))
    require(expected == source, "candidate Wheel/source parity changed")
    require(
        current_modules(root / "envs/bridge", source, sid) == source,
        "installed I5 shared Bridge Owner byte parity missing",
    )
    i7 = gate.helper("v0250-i7-connectors-disabled-register.py", "i5_post_i7_protected")
    i7.postflight()
    i5 = gate.helper("v0250-i5-provider-shared-preflight.py", "i5_post_owner_identity")
    i5.protected_tasks()
    i5.verify_candidates(i5.candidates(), installed=True)
    builder = helper("v0250-i2-stage.py", "i5_owner_import_probe")
    py = root / "envs/bridge/Scripts/python.exe"
    storage.check_private(py, directory=False, sid=sid)
    result = builder.run(
        [
            str(py),
            "-I",
            "-B",
            "-c",
            "from serverfs_mcp.windows_v25.runtime import run_independent_bridge_v2;"
            "from serverfs_mcp.windows_v25.cli import main;"
            "print('I5_OWNER_SEVEN_MODULE_IMPORTS_OK')",
        ],
        timeout=45,
    )
    require(result.strip() == b"I5_OWNER_SEVEN_MODULE_IMPORTS_OK", "installed Owner imports failed")
    print("I5_OWNER_PRIVATE_INSTALLED_SEVEN_MODULE_PARITY=PASS")
    print("I5_THREE_TASKS_AND_PRODUCTION_PROTECTED=PASS")


def main() -> None:
    global PHASE
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--audit", action="store_true")
    actions.add_argument("--stage", action="store_true")
    actions.add_argument("--postflight", action="store_true")
    selected = parser.parse_args()
    if selected.postflight:
        PHASE = "read-only-installed-postflight"
        postflight()
        print("I5_OWNER_OFFLINE_ROLLOUT_POSTFLIGHT=PASS")
        return

    PHASE = "exact-private-old-Wheel-and-candidate"
    gate, _wheel_gate, root, env, sid, source, old_bytes, previous, candidate, py = preflight()
    marker = root / "build" / MARKER
    require(not marker.exists() and not marker.is_symlink(), "one-use rollout marker occupied")
    print("I5_OWNER_PREINSTALL_OLD_AND_NEW_WHEELS=VERIFIED")
    if selected.audit:
        print("I5_OWNER_OFFLINE_ROLLOUT_AUDIT=PASS")
        return

    builder = helper("v0250-i2-stage.py", "i5_owner_private_offline_installer")
    require(builder.REPO == gate.REPO, "private builder repo differs")
    settings = dict(os.environ)
    settings["UV_LINK_MODE"] = "copy"
    command = install_args(py, candidate)
    PHASE = "offline-dry-run"
    builder.run([*command, "--dry-run"], env=settings, timeout=90)
    gate.native_preflight()
    print("I5_OWNER_OFFLINE_UPGRADE_DRY_RUN=PASS")

    PHASE = "one-use-marker"
    builder._new_dir(marker, sid)
    failed_phase = ""
    failed = False
    rollback_verified = True
    try:
        PHASE = "single-private-Bridge-owner-install"
        builder.run(command, env=settings, timeout=150)
        builder._protect_tree(env, sid)
        require(
            current_modules(env, source, sid) == source,
            "new private Owner not source-identical",
        )
        PHASE = "installed-imports-and-protected-postflight"
        postflight()
    except BaseException:  # noqa: BLE001 - no secret exception in output
        failed, failed_phase = True, PHASE
    if failed:
        PHASE = "restore-unique-retained-old-Wheel"
        try:
            builder.run(install_args(py, previous), env=settings, timeout=150)
            builder._protect_tree(env, sid)
            require(
                current_modules(env, source, sid) == old_bytes,
                "old Owner Wheel bytes not fully restored",
            )
            gate.native_preflight()
        except BaseException:  # noqa: BLE001 - retain explicit rollback unknown
            rollback_verified = False
        print("I5_OWNER_OLD_WHEEL_ROLLBACK=" + ("VERIFIED" if rollback_verified else "UNVERIFIED"))
        PHASE = failed_phase
        raise I5OwnerRolloutError("I5 one-use package replacement was refused") from None

    print("I5_OWNER_ONLY_OFFLINE_ROLLOUT=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - class/phase only, not private data
        print(
            "I5_OWNER_OFFLINE_ROLLOUT=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

"""One-use offline Wheel upgrade of ONLY two disabled nonproduction I7 owners.

Each installed serverfs_mcp package must have exactly one source delta:
windows_v25/tasks.py. The new protected Wheel was independently built and
compared against both old private rollback Wheels. No production, DNS,
credentials or Scheduler lifecycle mutation. On failure restore both exact
private old Wheels; if restore cannot be verified, fail visibly and stop.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

INSTANCE = "i7-public"
COMPONENTS = ("core", "cloudflare")
MARKER = "scheduler-failclosed-rollout-1"
PHASE = "entry"


class I7OfflineRolloutError(RuntimeError):
    """No private filesystem paths or secrets in printed errors."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I7OfflineRolloutError(message)


def installer():
    import importlib.util

    path = Path(__file__).with_name("v0250-i7-scheduler-failclosed-wheel-stage.py")
    spec = importlib.util.spec_from_file_location("i7_private_wheel_rollback", path)
    require(spec is not None and spec.loader is not None, "I7 wheel gate absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package_files(env: Path, source_modules: dict[str, bytes], sid: str) -> dict[str, bytes]:
    from serverfs_mcp.windows_v25 import storage

    pkg = env / "Lib/site-packages/serverfs_mcp"
    storage.check_private(pkg, directory=True, sid=sid)
    found = {}
    for name in source_modules:
        file = pkg / name
        require(file.is_file() and not file.is_symlink(), "installed module unavailable")
        storage.check_private(file, directory=False, sid=sid)
        found[name] = file.read_bytes()
    return found


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


def stage_inputs():
    from serverfs_mcp.windows_v25 import storage

    wheel_gate = installer()
    base, sid, modules, previous, gate = wheel_gate.installed_baseline()
    slot = base / "build" / wheel_gate.SLOT
    candidate = wheel_gate.only_wheel(slot, sid)
    wheel_gate.verify_candidate(candidate, modules, previous)
    require(wheel_gate.INSTANCE == INSTANCE, "private instance identity changed")
    for component in COMPONENTS:
        python = base / "envs" / component / "Scripts/python.exe"
        storage.check_private(python, directory=False, sid=sid)
        require(python.is_file() and not python.is_symlink(), "private owner Python unsafe")
    return wheel_gate, base, sid, modules, previous, candidate, gate


def installed_postflight():
    from serverfs_mcp.windows_v25 import storage

    wheel_gate = installer()
    base = storage.root(INSTANCE)
    sid = storage._sid()
    modules = wheel_gate.source_modules()
    target = wheel_gate.only_wheel(base / "build" / wheel_gate.SLOT, sid)
    old = {
        component: wheel_gate.only_wheel(base / "build" / (component + "-owner"), sid)
        for component in COMPONENTS
    }
    wheel_gate.verify_candidate(target, modules, old)
    gate = wheel_gate.helper("v0250-i7-connectors-disabled-register.py", "i7_rollout_postflight")
    gate.postflight()
    for component in COMPONENTS:
        env = base / "envs" / component
        storage.check_private(env, directory=True, sid=sid)
        require(package_files(env, modules, sid) == modules, "installed source parity drift")
    print("I7_TWO_PRIVATE_OWNER_SCHEDULER_SOURCE_PARITY=PASS")
    print("I7_ALL_THREE_DISABLED_AND_PROTECTED=PASS")


def run() -> None:
    global PHASE
    args = argparse.ArgumentParser()
    actions = args.add_mutually_exclusive_group(required=True)
    actions.add_argument("--audit", action="store_true")
    actions.add_argument("--stage", action="store_true")
    actions.add_argument("--postflight", action="store_true")
    choice = args.parse_args()

    if choice.postflight:
        PHASE = "read-only-installed-postflight"
        installed_postflight()
        print("I7_SCHEDULER_OFFLINE_ROLLOUT_POSTFLIGHT=PASS")
        return

    PHASE = "private-disabled-preflight-and-rollback-wheels"
    wheel_gate, base, sid, modules, old_wheels, candidate, gate = stage_inputs()
    marker = base / "build" / MARKER
    require(not marker.exists() and not marker.is_symlink(), "one-use rollout already occupied")
    print("I7_ROLLOUT_SOURCE_AND_OLD_WHEELS=VERIFIED")
    if choice.audit:
        print("I7_SCHEDULER_OFFLINE_ROLLOUT_AUDIT=PASS")
        return

    # Existing Windows uv helper is known to perform an offline, no-shell,
    # bounded subprocess and suppress its output on failure.
    helper = wheel_gate.helper("v0250-i2-stage.py", "i7_private_install_helper")
    require(helper.REPO == wheel_gate.REPO, "wheel helper checkout mismatch")
    env_vars = dict(os.environ)
    env_vars["UV_LINK_MODE"] = "copy"
    commands = {}
    for component in COMPONENTS:
        python = base / "envs" / component / "Scripts/python.exe"
        commands[component] = install_args(python, candidate)

    PHASE = "offline-dry-run-both-components"
    for component in COMPONENTS:
        helper.run([*commands[component], "--dry-run"], env=env_vars, timeout=90)
    # The dry run must not alter the installed old bytes or any owned task.
    wheel_gate.installed_baseline()
    print("I7_TWO_OWNER_OFFLINE_DRY_RUN=PASS")

    PHASE = "one-use-private-rollout-marker"
    helper._new_dir(marker, sid)
    attempted: list[str] = []
    failure: BaseException | None = None
    failed_phase = ""
    rollback_complete = True
    try:
        for component in COMPONENTS:
            PHASE = "install-only-private-" + component
            attempted.append(component)  # even partial installation must roll back
            helper.run(commands[component], env=env_vars, timeout=150)
            helper._protect_tree(base / "envs" / component, sid)
            require(
                package_files(base / "envs" / component, modules, sid) == modules,
                "new private owner source parity mismatch",
            )
            print("I7_" + component.upper() + "_PRIVATE_WHEEL_INSTALLED=PASS")
        PHASE = "both-owners-installed-postflight"
        installed_postflight()
    except BaseException as exc:  # noqa: BLE001 - no private exception content
        failure, failed_phase = exc, PHASE
    if failure is not None:
        PHASE = "private-wheel-rollback"
        for component in reversed(attempted):
            try:
                python = base / "envs" / component / "Scripts/python.exe"
                helper.run(
                    install_args(python, old_wheels[component]),
                    env=env_vars,
                    timeout=150,
                )
                helper._protect_tree(base / "envs" / component, sid)
                old_bytes = wheel_gate.archived_modules(old_wheels[component], set(modules))
                require(
                    package_files(base / "envs" / component, modules, sid) == old_bytes,
                    "restored source does not match retained wheel",
                )
            except BaseException:  # noqa: BLE001 - retain explicit unverified status
                rollback_complete = False
        try:
            gate.postflight()
        except BaseException:
            rollback_complete = False
        print(
            "I7_TWO_OWNER_OLD_WHEEL_ROLLBACK=" + ("VERIFIED" if rollback_complete else "UNVERIFIED")
        )
        PHASE = failed_phase
        raise I7OfflineRolloutError("one-use rollout failed after partial attempt") from None

    PHASE = "finished-offline"
    print("I7_SCHEDULER_ROLLOUT_TWO_OWNERS_OFFLINE=PASS")


if __name__ == "__main__":
    try:
        run()
    except BaseException as exc:  # noqa: BLE001 - fixed exception class only
        print(
            "I7_SCHEDULER_ROLLOUT=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

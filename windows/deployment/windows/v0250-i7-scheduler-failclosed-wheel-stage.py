"""Create-only, offline I7 Scheduler fail-closed wheel candidate (no installation).

Private, nonproduction I7 only. The two registered Connector owners and
the file-only Core remain Disabled/offline throughout. Build one protected
wheel after verifying the ONLY source-vs-installed change is tasks.py and
both old private wheels can restore their respective installed bytes.

No package installation, Scheduler lifecycle mutation, network, token or
production access. A partial one-use slot is retained for diagnosis, never overwritten.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import zipfile
from pathlib import Path

INSTANCE = "i7-public"
REPO = Path(__file__).resolve().parents[2]
SLOT = "scheduler-failclosed-wheel-1"
CHANGED = "windows_v25/tasks.py"
TARGETS = ("core", "cloudflare")
PHASE = "entry"


class I7WheelStageError(RuntimeError):
    """Sanitized failure; never print a private path or wheel data."""


def require(condition: bool, stage: str) -> None:
    if not condition:
        raise I7WheelStageError(stage)


def helper(name: str, label: str):
    file = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, file)
    require(spec is not None and spec.loader is not None, "fixed helper absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_modules() -> dict[str, bytes]:
    source = REPO / "src/serverfs_mcp"
    result = {}
    for file in source.rglob("*.py"):
        require(
            file.is_file() and not file.is_symlink(),
            "repository source is not a regular file",
        )
        result[file.relative_to(source).as_posix()] = file.read_bytes()
    require(CHANGED in result and len(result) >= 20, "source module inventory incomplete")
    return result


def only_wheel(directory: Path, sid: str) -> Path:
    from serverfs_mcp.windows_v25 import storage

    storage.check_private(directory, directory=True, sid=sid)
    wheels = tuple(directory.glob("serverfs_mcp-*.whl"))
    require(len(wheels) == 1, "expected exactly one retained source wheel")
    wheel = wheels[0]
    require(wheel.is_file() and not wheel.is_symlink(), "unsafe wheel path")
    storage.check_private(wheel, directory=False, sid=sid)
    return wheel


def archived_modules(wheel: Path, names: set[str]) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as archive:
        require(archive.testzip() is None, "wheel ZIP integrity failure")
        result = {name: archive.read("serverfs_mcp/" + name) for name in names}
    require(set(result) == names, "wheel module inventory mismatch")
    return result


def wheel_metadata_delta(previous: Path, candidate: Path) -> None:
    """Only changed Python module and wheel RECORD may differ."""
    with zipfile.ZipFile(previous) as old, zipfile.ZipFile(candidate) as new:
        old_names = set(old.namelist())
        new_names = set(new.namelist())
        require(old_names == new_names, "wheel members changed outside scoped fix")
        for member in sorted(old_names):
            if member == "serverfs_mcp/" + CHANGED or member.endswith(".dist-info/RECORD"):
                continue
            require(old.read(member) == new.read(member), "unexpected wheel payload change")


def installed_baseline():
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    gate = helper("v0250-i7-connectors-disabled-register.py", "i7_scheduler_wheel_scope")
    require(
        gate.INSTANCE == INSTANCE and gate.IDS == ("i7-edge-a", "i7-edge-b"),
        "I7 private identity mismatch",
    )
    gate.host()
    gate.postflight()
    sid = current_user_sid()
    require(sid == storage._sid(), "native I7 owner SID changed")
    base = storage.root(INSTANCE)
    modules = source_modules()
    old_wheels: dict[str, Path] = {}
    for component in TARGETS:
        env = base / "envs" / component
        pkg = env / "Lib/site-packages/serverfs_mcp"
        storage.check_private(env, directory=True, sid=sid)
        storage.check_private(pkg, directory=True, sid=sid)
        mismatches: set[str] = set()
        installed: dict[str, bytes] = {}
        for name, source_data in modules.items():
            file = pkg / name
            require(
                file.is_file() and not file.is_symlink(),
                "installed source module missing or unsafe",
            )
            storage.check_private(file, directory=False, sid=sid)
            data = file.read_bytes()
            installed[name] = data
            if data != source_data:
                mismatches.add(name)
        require(mismatches == {CHANGED}, "I7 source delta is not Scheduler-only")
        wheel = only_wheel(base / "build" / (component + "-owner"), sid)
        require(
            archived_modules(wheel, set(modules)) == installed,
            "old wheel does not reproduce installed source bytes",
        )
        old_wheels[component] = wheel
        print("I7_" + component.upper() + "_ONLY_SCHEDULER_SOURCE_DELTA=PASS")
    return base, sid, modules, old_wheels, gate


def verify_candidate(
    wheel: Path, modules: dict[str, bytes], previous_wheels: dict[str, Path]
) -> None:
    require(
        archived_modules(wheel, set(modules)) == modules,
        "new wheel does not match current source",
    )
    for previous in previous_wheels.values():
        wheel_metadata_delta(previous, wheel)


def main() -> None:
    global PHASE
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--audit", action="store_true")
    actions.add_argument("--stage", action="store_true")
    actions.add_argument("--postflight", action="store_true")
    args = parser.parse_args()

    PHASE = "native-i7-disabled-baseline-and-old-wheel"
    base, sid, modules, old_wheels, gate = installed_baseline()
    print("I7_REFRESH_PRIVATE_OLD_WHEEL_ROLLBACK_BASELINE=PASS")
    slot = base / "build" / SLOT
    if args.audit or args.stage:
        require(not slot.exists() and not slot.is_symlink(), "one-use wheel slot already occupied")
        if args.audit:
            print("I7_SCHEDULER_WHEEL_AUDIT=PASS")
            return

    installer = helper("v0250-i2-stage.py", "i7_scheduler_offline_wheel_helper")
    require(installer.REPO == REPO, "wheel builder checkout changed")
    if args.stage:
        PHASE = "create-one-private-wheel-slot"
        installer._new_dir(slot, sid)
        PHASE = "offline-wheel-build"
        wheel = installer._wheel(
            REPO,
            slot,
            "serverfs_mcp-*.whl",
            (CHANGED, "windows_v25/runtime.py"),
        )
        installer._protect_tree(slot, sid)
        verify_candidate(wheel, modules, old_wheels)
        print("I7_SCHEDULER_NEW_WHEEL_DIFF_ONLY_TASKS_AND_RECORD=PASS")

    PHASE = "wheel-only-postflight"
    wheel = only_wheel(slot, sid)
    verify_candidate(wheel, modules, old_wheels)
    gate.postflight()
    print("I7_SCHEDULER_WHEEL_CANDIDATE=VERIFIED")
    print("I7_SCHEDULER_NO_PRIVATE_PACKAGE_REINSTALL=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no private paths or token contents
        print(
            "I7_SCHEDULER_WHEEL=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

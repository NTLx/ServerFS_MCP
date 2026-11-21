"""Windows-native disposable real-Wheel crash/rollback evidence for v0.25.

One separate, unregistered I8 instance: no Task Scheduler manifest, Tunnel,
Provider, credential, network, or production package modification. Reuse the
existing private I5 old/new source-verified Wheels, but install ONLY into a
fresh user-private I8 uv Python venv. A subprocess installs the new Wheel,
intentionally damages ONE I8 module to model interrupted/partial package
state, persists an exact marker and terminates with os._exit(79) before
postflight/rollback. Another subprocess uses REAL offline uv to restore
the old Wheel and verifies every installed Python module and private DACL.

IMPORTANT: this exercises an actual process death and real uv recovery from
a deliberately corrupted installed package; it is NOT an OS kill while uv
itself is actively writing its installation transaction. Keep that distinct
full interruption requirement OPEN unless independently tested.

One-use --stage, one-use --exercise-once; --recover is a separate process.
No blanket deletion; retained private I8 evidence permits independent audit.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTANCE = "i8-wheel-recovery"
SLOT = "real-uv-crash-recovery-1"
CORRUPT = b"# I8 intentionally corrupted offline package module\n"
CRASH_CODE = 79
PHASE = "entry"


class I8RecoveryRefusal(RuntimeError):
    """No sensitive path, content, or wheel metadata shown to callers."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I8RecoveryRefusal(reason)


def helper(name: str, label: str):
    source = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "fixed helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native_protected() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "current-user native Windows -I -B required",
    )
    i5 = helper("v0250-i5-public-connector-retire.py", "i8_source_protection")
    i5.postflight()
    production = storage.load("core")
    require(_current_identity(production) is not None, "protected production Core unavailable")


def archives() -> tuple[Path, Path, dict[str, bytes], dict[str, bytes]]:
    """Accept only source-backed, seven-module-delta private I5 Wheels."""
    from serverfs_mcp.windows_v25 import storage

    i5 = helper("v0250-i5-owner-private-wheel-audit.py", "i8_source_archive_reader")
    gate = helper("v0250-i5-owner-seven-module-wheel-stage.py", "i8_source_delta_gate")
    sid = storage._sid()
    build = storage.root("i5-shared") / "build"
    storage.check_private(build, directory=True, sid=sid)
    new = gate.only_private_wheel(build / gate.SLOT, sid)
    source = i5.source_modules()
    require(i5.wheel_modules(new, set(source)) == source, "source candidate Wheel mismatch")

    candidates = tuple(build.rglob("serverfs_mcp-*.whl"))
    require(1 < len(candidates) <= 35, "I5 private historical Wheel scan not bounded")
    approved: list[tuple[Path, dict[str, bytes]]] = []
    for wheel in candidates:
        require(wheel.is_file() and not wheel.is_symlink(), "historical private Wheel path unsafe")
        storage.check_private(wheel, directory=False, sid=sid)
        if wheel == new:
            continue
        try:
            gate.verify_archive_delta(wheel, new, allowed=i5.EXPECTED_DELTA)
            old_bytes = i5.wheel_modules(wheel, set(source))
        except (gate.I5WheelStageError, ValueError, KeyError, zipfile.BadZipFile):
            continue
        require(
            {name for name in source if source[name] != old_bytes[name]} == i5.EXPECTED_DELTA,
            "I5 previous Wheel no longer has seven exactly approved changes",
        )
        approved.append((wheel, old_bytes))
    require(len(approved) == 1, "I5 exact historical rollback Wheel is ambiguous")
    return approved[0][0], new, approved[0][1], source


def paths():
    from serverfs_mcp.windows_v25 import storage

    root = storage.root(INSTANCE)
    return root, root / "build" / SLOT, root / "envs/core", root / "envs/core/Scripts/python.exe"


def load_intent(slot: Path, name: str, sid: str) -> dict:
    from serverfs_mcp.windows_v25 import storage

    path = slot / name
    storage.check_private(path, directory=False, sid=sid)
    data = json.loads(path.read_text(encoding="utf-8"))
    require(
        isinstance(data, dict) and data.get("schema") == 1 and data.get("instance") == INSTANCE,
        "I8 private marker content invalid",
    )
    return data


def mark(slot: Path, name: str, sid: str) -> None:
    installer = helper("v0250-i2-stage.py", "i8_private_marker")
    data = (json.dumps({"schema": 1, "instance": INSTANCE}, sort_keys=True) + "\n").encode()
    installer._new_private_file(slot / name, data, sid)


def wheel_install(venv: Path, wheel: Path, sid: str) -> None:
    import os

    builder = helper("v0250-i2-stage.py", "i8_real_uv_installer")
    env = dict(os.environ)
    env["UV_LINK_MODE"] = "copy"
    py = venv / "Scripts/python.exe"
    builder.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--reinstall",
            "--python",
            str(py),
            str(wheel),
        ],
        env=env,
        timeout=120,
    )
    builder._protect_tree(venv, sid)


def installed(venv: Path, required: dict[str, bytes], sid: str) -> bool:
    from serverfs_mcp.windows_v25 import storage

    pkg = venv / "Lib/site-packages/serverfs_mcp"
    storage.check_private(venv, directory=True, sid=sid)
    storage.check_private(pkg, directory=True, sid=sid)
    for name, content in required.items():
        path = pkg / name
        storage.check_private(path, directory=False, sid=sid)
        if path.read_bytes() != content:
            return False
    return True


def private_state() -> tuple[Path, Path, Path, Path, str]:
    from serverfs_mcp.windows_v25 import storage

    root, slot, venv, py = paths()
    sid = storage._sid()
    for path in (root, root / "envs", venv, root / "build", slot):
        storage.check_private(path, directory=True, sid=sid)
    storage.check_private(py, directory=False, sid=sid)
    require(py.is_file() and not py.is_symlink(), "disposable I8 interpreter absent")
    load_intent(slot, "prepared.json", sid)
    return root, slot, venv, py, sid


def audit() -> None:
    native_protected()
    previous, current, old, new = archives()
    require(previous != current and old != new, "no distinct approved I5 Wheels")
    base, _slot, _venv, _py = paths()
    require(not base.exists() and not base.is_symlink(), "I8 one-use fixture already occupied")
    print("I8_TWO_REAL_I5_WHEELS_BYTE_AND_DELTA=VERIFIED")
    print("I8_PRIVATE_ISOLATED_NO_SCHEDULER_SLOT=PASS")


def stage() -> None:
    global PHASE
    from serverfs_mcp.windows_v25 import storage

    audit()
    oldwheel, _newwheel, old, _new = archives()
    PHASE = "create-only-new-I8-private-directories"
    storage.prepare("core", INSTANCE)
    root, slot, venv, py = paths()
    sid = storage._sid()
    installer = helper("v0250-i2-stage.py", "i8_native_stage_builder")
    installer._new_dir(root / "build", sid)
    installer._new_dir(slot, sid)
    PHASE = "offline-real-uv-venv-creation"
    require(not any(venv.iterdir()), "disposable venv occupied")
    installer.run(["uv", "venv", "--offline", "--python", "3.12.10", str(venv)], timeout=80)
    installer._protect_tree(venv, sid)
    storage.check_private(py, directory=False, sid=sid)
    PHASE = "offline-real-old-Wheel-install"
    wheel_install(venv, oldwheel, sid)
    require(installed(venv, old, sid), "old Wheel not actually installed in I8")
    mark(slot, "prepared.json", sid)
    native_protected()
    print("I8_REAL_UV_OLD_WHEEL_STAGED=PASS")


def crash_child() -> None:
    global PHASE
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    native_protected()
    _oldwheel, newwheel, old, current = archives()
    _root, slot, venv, _py, sid = private_state()
    require(installed(venv, old, sid), "I8 old Wheel not at crash initial state")
    require(not (slot / "crash-intent.json").exists(), "crash already attempted")
    require(not (slot / "recovered.json").exists(), "I8 recovery already performed")
    mark(slot, "crash-intent.json", sid)
    PHASE = "actual-new-Wheel-uv-install"
    wheel_install(venv, newwheel, sid)
    require(installed(venv, current, sid), "new Wheel not actually installed in I8")
    PHASE = "deliberate-I8-only-partial-module-corruption"
    target = venv / "Lib/site-packages/serverfs_mcp/windows_v25/tasks.py"
    storage.check_private(target, directory=False, sid=sid)
    target.write_bytes(CORRUPT)  # ONLY disposable I8 venv, never I5 or production
    protect_new_file(target, sid)
    require(target.read_bytes() == CORRUPT, "I8 partial-corruption proof not applied")
    mark(slot, "crash-applied.json", sid)
    print("I8_REAL_UV_NEW_WHEEL_AND_CORRUPT_STATE=VERIFIED", flush=True)
    print("I8_TEST_PROCESS_DEATH_BEFORE_ROLLBACK=INTENTIONAL", flush=True)
    os._exit(CRASH_CODE)  # exact native child process death, no finally or cleanup


def recover() -> None:
    global PHASE
    native_protected()
    oldwheel, _newwheel, old, current = archives()
    _root, slot, venv, _py, sid = private_state()
    load_intent(slot, "crash-intent.json", sid)
    load_intent(slot, "crash-applied.json", sid)
    require(not (slot / "recovered.json").exists(), "I8 already recovered")
    # Fail closed unless this exact expected partial-install corruption exists.
    package = venv / "Lib/site-packages/serverfs_mcp/windows_v25/tasks.py"
    require(package.read_bytes() == CORRUPT, "I8 crash artifact mismatch")
    require(not installed(venv, old, sid), "I8 old Wheel unexpectedly still installed")
    PHASE = "actual-uv-offline-old-Wheel-rollback"
    wheel_install(venv, oldwheel, sid)
    require(installed(venv, old, sid), "old Wheel not restored byte-for-byte")
    require(not installed(venv, current, sid), "new Wheel still installed")
    mark(slot, "recovered.json", sid)
    native_protected()
    print("I8_REAL_UV_BYTE_EXACT_OLD_WHEEL_RECOVERY=PASS")


def postflight() -> None:
    native_protected()
    _oldwheel, _newwheel, old, current = archives()
    _root, slot, venv, _py, sid = private_state()
    for name in ("crash-intent.json", "crash-applied.json", "recovered.json"):
        load_intent(slot, name, sid)
    require(installed(venv, old, sid), "final I8 old Wheel parity failed")
    require(not installed(venv, current, sid), "final I8 matches unintended new Wheel")
    from serverfs_mcp.windows_v25 import storage

    # No instance manifest has ever been written, no task has been installed.
    core_home = storage.home("core", INSTANCE)
    storage.check_private(core_home, directory=True, sid=sid)
    require(not any(core_home.iterdir()), "I8 unexpectedly contains Core service files")
    # Only a defensive no-manifest claim; Scheduler tasks are checked through
    # exact native global inventory in the protected I5/I7 postflights above.
    require(not (core_home / "service.json").exists(), "I8 service manifest present")
    print("I8_REAL_UV_ROLLBACK_PRIVATE_SOURCE_PARITY=PASS")
    print("I8_NO_NEW_SCHEDULER_MANIFEST=PASS")
    print("I8_PRODUCTION_AND_I5_I7_PROTECTED=PASS")


def exercise() -> None:
    global PHASE
    native_protected()
    _root, slot, venv, _py, sid = private_state()
    _oldwheel, _newwheel, old, _new = archives()
    require(installed(venv, old, sid), "I8 old installed baseline changed")
    require(
        not any(
            (slot / name).exists()
            for name in ("crash-intent.json", "crash-applied.json", "recovered.json")
        ),
        "one-use crash exercise occupied",
    )
    PHASE = "spawn-failing-independent-native-process"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    child = subprocess.run(
        [str(sys.executable), "-I", "-B", str(Path(__file__)), "--crash-child"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=220,
        creationflags=flags,
    )
    require(
        child.returncode == CRASH_CODE
        and b"I8_REAL_UV_NEW_WHEEL_AND_CORRUPT_STATE=VERIFIED" in child.stdout
        and b"I8_TEST_PROCESS_DEATH_BEFORE_ROLLBACK=INTENTIONAL" in child.stdout,
        "I8 crash-child did not prove expected separate process termination",
    )
    load_intent(slot, "crash-applied.json", sid)
    require(not installed(venv, old, sid), "I8 crash persisted no package drift")
    print("I8_REAL_CHILD_PROCESS_CRASH_EXIT_79=PASS")

    PHASE = "spawn-different-native-rollback-process"
    rollback = subprocess.run(
        [str(sys.executable), "-I", "-B", str(Path(__file__)), "--recover"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=220,
        creationflags=flags,
    )
    require(
        rollback.returncode == 0
        and b"I8_REAL_UV_BYTE_EXACT_OLD_WHEEL_RECOVERY=PASS" in rollback.stdout,
        "independent I8 uv recovery process failed",
    )
    PHASE = "independent-native-postflight"
    completed = subprocess.run(
        [str(sys.executable), "-I", "-B", str(Path(__file__)), "--postflight"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=160,
        creationflags=flags,
    )
    require(
        completed.returncode == 0
        and b"I8_REAL_UV_ROLLBACK_PRIVATE_SOURCE_PARITY=PASS" in completed.stdout
        and b"I8_PRODUCTION_AND_I5_I7_PROTECTED=PASS" in completed.stdout,
        "independent I8 final rollback postflight refused",
    )
    print("I8_REAL_OFFLINE_UV_CRASH_AND_RECOVERY=PASS")


def main() -> None:
    global PHASE
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--audit", action="store_true")
    action.add_argument("--stage", action="store_true")
    action.add_argument("--exercise-once", action="store_true")
    action.add_argument("--crash-child", action="store_true")
    action.add_argument("--recover", action="store_true")
    action.add_argument("--postflight", action="store_true")
    args = parser.parse_args()
    PHASE = "scoped-native-test-entry"
    if args.audit:
        audit()
    elif args.stage:
        stage()
    elif args.exercise_once:
        exercise()
    elif args.crash_child:
        crash_child()
    elif args.recover:
        recover()
    else:
        postflight()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no raw private details
        print(
            "I8_NATIVE_ROLLBACK=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

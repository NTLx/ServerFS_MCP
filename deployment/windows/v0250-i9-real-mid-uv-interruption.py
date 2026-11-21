"""One disposable Windows uv child-process mid-transaction kill and real rollback.

Not the previous I8 post-install corruption simulation: NO module is damaged
by the harness. Only terminate the exact newly spawned real offline uv process
if an old package module is observed missing while uv is STILL ALIVE. If uv
finishes too fast, preserve INCONCLUSIVE rather than fabricating interruption.
All writes target a never-registered ordinary-user I9 private venv; old/new
source Wheels are independently verified by the existing I8 audit helper.
One-use markers remain for forensic evidence. No Tunnel, Provider or Scheduler.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

INSTANCE = "i9-uv-midkill"
SLOT = "mid-uv-once"
MARKERS = frozenset({"prepared", "attempted", "killed", "completed", "recovered"})
PHASE = "entry"


class I9Refusal(RuntimeError):
    """Do not disclose private package paths, code or credentials."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I9Refusal(message)


def module(name: str, label: str):
    source = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "I9 source helper missing")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def i8():
    return module("v0250-i8-real-wheel-crash-recovery.py", "i9_verified_i8_wheel_helpers")


def state_paths():
    from serverfs_mcp.windows_v25 import storage

    root = storage.root(INSTANCE)
    return root, root / "build" / SLOT, root / "envs/core"


def native_gate() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "native ordinary-user Windows -I -B required",
    )
    i8().native_protected()


def marker(slot: Path, key: str, sid: str, *, create: bool = False) -> bool:
    from serverfs_mcp.windows_v25 import storage

    require(key in MARKERS, "unknown I9 marker")
    path = slot / (key + ".json")
    document = b'{"instance":"i9-uv-midkill","schema":1}\n'
    if create:
        private = module("v0250-i2-stage.py", "i9_sealed_private_marker")
        private._new_private_file(path, document, sid)
    if not path.exists():
        require(not path.is_symlink(), "I9 marker symlink refused")
        return False
    storage.check_private(path, directory=False, sid=sid)
    require(
        path.is_file() and not path.is_symlink() and path.read_bytes() == document,
        "I9 marker differs",
    )
    return True


def private_state():
    from serverfs_mcp.windows_v25 import storage

    root, slot, venv = state_paths()
    sid = storage._sid()
    for directory in (root, root / "build", slot, root / "envs", venv):
        storage.check_private(directory, directory=True, sid=sid)
    python = venv / "Scripts/python.exe"
    storage.check_private(python, directory=False, sid=sid)
    require(python.is_file() and not python.is_symlink(), "I9 Python absent")
    require(marker(slot, "prepared", sid), "I9 private stage absent")
    return slot, venv, python, sid


def audit() -> None:
    native_gate()
    oldwheel, newwheel, old, new = i8().archives()
    require(oldwheel != newwheel and old != new, "I9 source Wheels not distinct")
    root, _slot, _venv = state_paths()
    require(not root.exists() and not root.is_symlink(), "I9 slot not fresh")
    print("I9_TWO_REAL_OFFLINE_WHEELS=VERIFIED")
    print("I9_PRODUCTION_AND_NONPROD_OWNERS_PROTECTED=PASS")


def stage() -> None:
    from serverfs_mcp.windows_v25 import storage

    audit()
    wheel = i8()
    oldwheel, _newwheel, old, _new = wheel.archives()
    private = module("v0250-i2-stage.py", "i9_offline_private_stager")
    storage.prepare("core", INSTANCE)
    root, slot, venv = state_paths()
    sid = storage._sid()
    private._new_dir(root / "build", sid)
    private._new_dir(slot, sid)
    require(not any(venv.iterdir()), "I9 venv already has content")
    private.run(["uv", "venv", "--offline", "--python", "3.12.10", str(venv)], timeout=80)
    private._protect_tree(venv, sid)
    wheel.wheel_install(venv, oldwheel, sid)
    require(wheel.installed(venv, old, sid), "I9 old Wheel byte parity failed")
    marker(slot, "prepared", sid, create=True)
    native_gate()
    print("I9_PRIVATE_OLD_WHEEL_STAGE=PASS")


def recover() -> None:
    global PHASE

    native_gate()
    wheel = i8()
    oldwheel, _newwheel, old, new = wheel.archives()
    slot, venv, _py, sid = private_state()
    require(marker(slot, "attempted", sid), "I9 no actual attempt marker")
    killed = marker(slot, "killed", sid)
    completed = marker(slot, "completed", sid)
    require(killed != completed, "I9 terminal uv classification ambiguous")
    require(not marker(slot, "recovered", sid), "I9 recovery already completed")
    PHASE = "separate-real-uv-offline-recover"
    wheel.wheel_install(venv, oldwheel, sid)
    require(wheel.installed(venv, old, sid), "I9 restored old bytes differ")
    require(not wheel.installed(venv, new, sid), "I9 still matches new archive")
    marker(slot, "recovered", sid, create=True)
    print("I9_REAL_UV_SEPARATE_OLD_WHEEL_RECOVERY=PASS")


def postflight() -> None:
    from serverfs_mcp.windows_v25 import storage

    native_gate()
    wheel = i8()
    _oldwheel, _newwheel, old, new = wheel.archives()
    slot, venv, _py, sid = private_state()
    require(marker(slot, "attempted", sid), "I9 attempt marker absent")
    require(marker(slot, "recovered", sid), "I9 recovery marker absent")
    killed = marker(slot, "killed", sid)
    completed = marker(slot, "completed", sid)
    require(killed != completed, "I9 terminal classification invalid")
    require(wheel.installed(venv, old, sid), "I9 old Wheel byte or DACL parity drift")
    require(not wheel.installed(venv, new, sid), "I9 new Wheel still installed")
    home = storage.home("core", INSTANCE)
    storage.check_private(home, directory=True, sid=sid)
    require(
        not any(home.iterdir()) and not (home / "service.json").exists(),
        "I9 unexpectedly registered a service",
    )
    print("I9_PRIVATE_BYTE_EXACT_OLD_WHEEL_AND_DACL=PASS")
    print("I9_NO_SCHEDULER_MANIFEST_OR_SERVICE=PASS")
    print("I9_PRODUCTION_AND_I5_I7_PROTECTED=PASS")
    print("I9_REAL_MID_UV_WINDOW=" + ("OBSERVED" if killed else "NOT_OBSERVED"))


def exercise_once() -> None:
    global PHASE

    native_gate()
    wheel = i8()
    _oldwheel, newwheel, old, _new = wheel.archives()
    slot, venv, py, sid = private_state()
    require(wheel.installed(venv, old, sid), "I9 staged old bytes differ")
    require(
        all(
            not marker(slot, key, sid) for key in ("attempted", "killed", "completed", "recovered")
        ),
        "I9 attempted one-use operation already",
    )
    target = venv / "Lib/site-packages/serverfs_mcp/windows_v25/tasks.py"
    require(target.is_file() and not target.is_symlink(), "I9 original module absent")
    PHASE = "commit-one-use-attempt-before-spawn"
    marker(slot, "attempted", sid, create=True)
    env = dict(os.environ)
    env.update(UV_LINK_MODE="copy", UV_NO_CACHE="1", UV_NO_PROGRESS="1")
    PHASE = "spawn-only-new-offline-I9-uv-child"
    process = subprocess.Popen(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--reinstall",
            "--python",
            str(py),
            str(newwheel),
        ],
        cwd=str(venv),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    killed = False
    try:
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            # An installed *old* module actually missing during a live uv
            # replacement transition is the required native OS observation.
            if not target.exists() and process.poll() is None:
                try:
                    process.kill()  # exact Popen child only; no host PID search
                except OSError:
                    pass
                process.wait(timeout=15)
                killed = process.returncode != 0
                break
            time.sleep(0.001)
        if process.poll() is None:
            process.kill()  # timeout; never count a timeout as observed
        code = process.wait(timeout=18)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=18)

    PHASE = "sealed-mid-uv-observation"
    if killed:
        require(code != 0, "I9 unexpected successful killed uv")
        marker(slot, "killed", sid, create=True)
        print("I9_REAL_UV_KILLED_WHILE_OLD_MODULE_MISSING=PASS")
    else:
        marker(slot, "completed", sid, create=True)
        print("I9_REAL_UV_INTERRUPTION=INCONCLUSIVE")

    PHASE = "separate-native-rollback-process"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    rollback = subprocess.run(
        [str(sys.executable), "-I", "-B", str(Path(__file__)), "--recover"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=240,
        creationflags=flags,
    )
    require(
        rollback.returncode == 0
        and b"I9_REAL_UV_SEPARATE_OLD_WHEEL_RECOVERY=PASS" in rollback.stdout,
        "I9 exact separate rollback failed",
    )
    PHASE = "independent-postflight-process"
    post = subprocess.run(
        [str(sys.executable), "-I", "-B", str(Path(__file__)), "--postflight"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=140,
        creationflags=flags,
    )
    require(
        post.returncode == 0
        and b"I9_PRIVATE_BYTE_EXACT_OLD_WHEEL_AND_DACL=PASS" in post.stdout
        and b"I9_PRODUCTION_AND_I5_I7_PROTECTED=PASS" in post.stdout,
        "I9 independent protection postflight failed",
    )
    print("I9_ISOLATED_REAL_UV_RECOVERY_AND_POSTFLIGHT=PASS")
    require(killed, "native mid-uv replacement window was not observed")
    print("I9_REAL_UV_MID_TRANSACTION_INTERRUPT_E2E=PASS")


def main() -> None:
    global PHASE
    choices = argparse.ArgumentParser()
    group = choices.add_mutually_exclusive_group(required=True)
    for name in ("audit", "stage", "exercise-once", "recover", "postflight"):
        group.add_argument("--" + name, action="store_true")
    args = choices.parse_args()
    PHASE = "native-I9-disposable-entry"
    if args.audit:
        audit()
    elif args.stage:
        stage()
    elif args.exercise_once:
        exercise_once()
    elif args.recover:
        recover()
    else:
        postflight()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print(
            "I9_NATIVE_MID_UV=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

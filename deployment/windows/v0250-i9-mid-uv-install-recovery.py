"""Native Windows I9: kill a real uv subprocess during private Wheel installation.

I9 is a disposable, unregistered, current-SID-private Core home and venv.
It reads immutable, source-audited I5 old/new Wheels, pads only a COPY
of the new Wheel with inert data files and a correct RECORD, and uses actual
offline uv install. The controller terminates ONLY its own live uv Popen
process handle after verified *process I/O writes*, not by PID/name search.
Recovery is a separate process that reinstalls the original exact old Wheel.
This does not simulate power loss or prove a system-wide filesystem crash.

--audit is read-only; --stage and --exercise-once are each once only.
--recover is only accepted after a confirmed mid-command termination marker.
Never rerun a one-use step if it fails; use --status to inspect evidence.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

INSTANCE = "i9-uv-midwrite"
SLOT = "real-uv-midwrite-1"
PADDING_COUNT = 384
PADDING_BYTES = 256 * 1024
MIN_ACTIVE_WRITE_BYTES = 8 * 1024 * 1024
UV_TERMINATE_CODE = 197
PHASE = "entry"


class I9MidwriteError(RuntimeError):
    """I9 crash/rollback refused, without private artifact or process details."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I9MidwriteError(reason)


def previous_gate():
    source = Path(__file__).with_name("v0250-i8-real-wheel-crash-recovery.py")
    spec = importlib.util.spec_from_file_location("windows_i9_source_audited_i8_helper", source)
    require(spec is not None and spec.loader is not None, "fixed I8 source gate absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "native non-admin Windows current-user -I -B required",
    )
    previous_gate().native_protected()


def paths() -> tuple[Path, Path, Path, Path]:
    from serverfs_mcp.windows_v25 import storage

    root = storage.root(INSTANCE)
    return (
        root,
        root / "build" / SLOT,
        root / "envs" / "core",
        root / "envs" / "core" / "Scripts" / "python.exe",
    )


def marker(slot: Path, name: str, sid: str) -> None:
    """Explicit I9 marker; never reuse I8's hard-coded fixture ID."""
    import json

    from serverfs_mcp.windows_v25 import storage

    builder = previous_gate().helper("v0250-i2-stage.py", "i9_private_marker")
    payload = (json.dumps({"schema": 1, "instance": INSTANCE}, sort_keys=True) + "\n").encode()
    builder._new_private_file(slot / name, payload, sid)
    storage.check_private(slot / name, directory=False, sid=sid)


def check_marker(slot: Path, name: str, sid: str) -> None:
    import json

    from serverfs_mcp.windows_v25 import storage

    path = slot / name
    storage.check_private(path, directory=False, sid=sid)
    require(
        json.loads(path.read_text("utf-8")) == {"schema": 1, "instance": INSTANCE},
        "disposable I9 marker integrity mismatch",
    )


def pad_name(index: int) -> str:
    require(0 <= index < PADDING_COUNT, "I9 pad index outside fixed range")
    return f"serverfs_mcp/_i9_uv_padding_{index:04d}.bin"


def stress_wheel_path(slot: Path, approved_new: Path) -> Path:
    return slot / approved_new.name


def _record_line(filename: str, data: bytes) -> list[str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return [filename, "sha256=" + digest, str(len(data))]


def create_stress_wheel(original: Path, dest: Path, sid: str) -> None:
    """Write a valid distribution Wheel, changing only inert data and RECORD.

    The new Wheel's *actual source modules* remain the source-verified I5
    candidate bytes; the extra ballast is never executable or imported.
    """
    import csv

    from serverfs_mcp.windows_v25 import storage

    require(not dest.exists() and not dest.is_symlink(), "I9 candidate Wheel already present")
    require(
        shutil.disk_usage(dest.parent).free > 768 * 1024 * 1024,
        "I9 free disk headroom insufficient",
    )
    temp = dest.with_name(dest.name + ".creating")
    require(not temp.exists() and not temp.is_symlink(), "I9 Wheel temp occupied")
    with zipfile.ZipFile(original, "r") as old:
        original_names = old.namelist()
        require(
            len(original_names) == len(set(original_names))
            and all("\\" not in name and not name.startswith("/") for name in original_names),
            "I5 Wheel unsafe member names",
        )
        records = [name for name in original_names if name.endswith(".dist-info/RECORD")]
        require(len(records) == 1, "I5 Wheel RECORD ambiguous")
        rec = records[0]
        require(
            all(not name.startswith("serverfs_mcp/_i9_uv_padding_") for name in original_names),
            "I5 baseline already contains I9 ballast",
        )
        source_names = [name for name in original_names if name != rec]
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        with zipfile.ZipFile(temp, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as newer:
            for name in source_names:
                data = old.read(name)
                newer.writestr(name, data)
                writer.writerow(_record_line(name, data))
            for index in range(PADDING_COUNT):
                name = pad_name(index)
                data = os.urandom(PADDING_BYTES)  # incompressible; bounded I9-only disk stress
                newer.writestr(name, data)
                writer.writerow(_record_line(name, data))
            writer.writerow([rec, "", ""])
            newer.writestr(rec, output.getvalue().encode("utf-8"))
    from serverfs_mcp.windows_private_artifacts import protect_new_file

    protect_new_file(temp, sid)
    storage.check_private(temp, directory=False, sid=sid)
    os.replace(temp, dest)
    storage.check_private(dest, directory=False, sid=sid)


def check_stress_wheel(original: Path, padded: Path, sid: str) -> None:
    from serverfs_mcp.windows_v25 import storage

    storage.check_private(padded, directory=False, sid=sid)
    require(
        padded.is_file()
        and not padded.is_symlink()
        and 75 * 1024 * 1024 < padded.stat().st_size < 160 * 1024 * 1024,
        "I9 stress Wheel size outside bounded envelope",
    )
    with zipfile.ZipFile(original) as old, zipfile.ZipFile(padded) as new:
        names = set(old.namelist())
        extras = {pad_name(i) for i in range(PADDING_COUNT)}
        original_records = [name for name in names if name.endswith(".dist-info/RECORD")]
        require(
            len(original_records) == 1 and set(new.namelist()) == names | extras,
            "I9 Wheel inventory mismatch",
        )
        for name in names - set(original_records):
            require(new.read(name) == old.read(name), "I9 original Wheel payload drift")
        require(
            new.read(pad_name(0)) and new.read(pad_name(PADDING_COUNT - 1)),
            "I9 added payload missing",
        )
        import csv

        rows = list(csv.reader(io.StringIO(new.read(original_records[0]).decode("utf-8"))))
        require(
            len(rows) == len(new.namelist())
            and {row[0] for row in rows} == set(new.namelist())
            and all(len(row) == 3 for row in rows),
            "I9 RECORD inventory mismatch",
        )


def private_state() -> tuple[Path, Path, Path, str]:
    from serverfs_mcp.windows_v25 import storage

    root, slot, venv, py = paths()
    sid = storage._sid()
    for directory in (root, root / "build", slot, root / "envs", venv):
        storage.check_private(directory, directory=True, sid=sid)
    storage.check_private(py, directory=False, sid=sid)
    require(py.is_file() and not py.is_symlink(), "I9 private venv absent")
    check_marker(slot, "prepared.json", sid)
    return slot, venv, py, sid


def audit() -> None:
    native()
    prev = previous_gate()
    old, new, old_bytes, new_bytes = prev.archives()
    require(old != new and old_bytes != new_bytes, "I9 approved baseline Wheels missing")
    root, _, _, _ = paths()
    require(not root.exists() and not root.is_symlink(), "I9 fixture already staged")
    print("I9_BASELINE_TWO_REAL_SOURCE_VERIFIED_WHEELS=PASS")
    print("I9_DISPOSABLE_UNREGISTERED_ROOT_AVAILABLE=PASS")


def stage() -> None:
    global PHASE
    from serverfs_mcp.windows_v25 import storage

    audit()
    old, new, old_bytes, _ = previous_gate().archives()
    PHASE = "private-I9-create-only-stage"
    storage.prepare("core", INSTANCE)
    root, slot, venv, py = paths()
    sid = storage._sid()
    builder = previous_gate().helper("v0250-i2-stage.py", "i9_native_offline_venv_builder")
    builder._new_dir(root / "build", sid)
    builder._new_dir(slot, sid)
    PHASE = "private-I9-offline-uv-venv"
    require(not any(venv.iterdir()), "I9 Core venv already occupied")
    builder.run(["uv", "venv", "--offline", "--python", "3.12.10", str(venv)], timeout=100)
    builder._protect_tree(venv, sid)
    storage.check_private(py, directory=False, sid=sid)
    PHASE = "private-I9-old-Wheel-installed"
    previous_gate().wheel_install(venv, old, sid)
    require(previous_gate().installed(venv, old_bytes, sid), "I9 old Wheel stage parity mismatch")
    PHASE = "private-I9-inert-stress-Wheel-build"
    stress = stress_wheel_path(slot, new)
    create_stress_wheel(new, stress, sid)
    check_stress_wheel(new, stress, sid)
    marker(slot, "prepared.json", sid)
    native()
    print("I9_PRIVATE_OLD_WHEEL_AND_INERT_STRESS_WHEEL_STAGED=PASS")


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


def write_bytes_from_owned_handle(handle: int) -> int:
    counters = IO_COUNTERS()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetProcessIoCounters.argtypes = (ctypes.c_void_p, ctypes.POINTER(IO_COUNTERS))
    kernel.GetProcessIoCounters.restype = ctypes.c_int
    require(
        bool(kernel.GetProcessIoCounters(ctypes.c_void_p(handle), ctypes.byref(counters))),
        "I9 owned uv handle I/O counters unavailable",
    )
    return counters.WriteTransferCount


def terminate_only_owned_uv(process: subprocess.Popen) -> None:
    """No machine-wide PID search or process-name termination; only Popen's private handle."""
    require(process.poll() is None, "uv already exited before kill")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    kernel.TerminateProcess.restype = ctypes.c_int
    require(
        bool(kernel.TerminateProcess(ctypes.c_void_p(int(process._handle)), UV_TERMINATE_CODE)),
        "I9 uv handle termination refused",
    )
    require(process.wait(timeout=20) == UV_TERMINATE_CODE, "I9 uv exit code not attributed to kill")


def exercise_once() -> None:
    global PHASE
    native()
    prev = previous_gate()
    old, new, old_bytes, _ = prev.archives()
    slot, venv, py, sid = private_state()
    check_stress_wheel(new, stress_wheel_path(slot, new), sid)
    require(prev.installed(venv, old_bytes, sid), "I9 old-Wheel baseline drift")
    require(
        not any(
            (slot / name).exists()
            for name in ("attempted.json", "terminated-midwrite.json", "recovered.json")
        ),
        "I9 one-use uv interruption already attempted",
    )
    marker(slot, "attempted.json", sid)
    PHASE = "start-real-uv-offline-disposable-install"
    env = dict(os.environ)
    env.update(UV_LINK_MODE="copy", UV_NO_CACHE="1")
    wheel = stress_wheel_path(slot, new)
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
            str(wheel),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    killed = False
    written = 0
    try:
        PHASE = "observe-owned-uv-active-write-transfer"
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            written = write_bytes_from_owned_handle(int(process._handle))
            if written >= MIN_ACTIVE_WRITE_BYTES and process.poll() is None:
                PHASE = "terminate-only-active-uv-owned-process-handle"
                terminate_only_owned_uv(process)
                killed = True
                break
            time.sleep(0.004)
        require(killed, "uv completed/no proven active transaction write before interruption")
    finally:
        # Never leave an untracked installer running on failure/timeout.
        if process.poll() is None:
            terminate_only_owned_uv(process)
    require(written >= MIN_ACTIVE_WRITE_BYTES, "uv write counter below evidence threshold")
    PHASE = "durable-midwrite-terminated-evidence"
    marker(slot, "terminated-midwrite.json", sid)
    print("I9_REAL_UV_PROCESS_LIVE_WRITE_TRANSFER_OBSERVED=PASS")
    print("I9_ONLY_OWNED_UV_PROCESS_TERMINATED_EXIT_197=PASS")

    PHASE = "separate-recovery-native-process"
    worker = subprocess.run(
        [str(sys.executable), "-I", "-B", str(Path(__file__)), "--recover"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=220,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(
        worker.returncode == 0 and b"I9_REAL_UV_OLD_WHEEL_RECOVERED=PASS" in worker.stdout,
        "I9 independent Wheel rollback subprocess refused",
    )
    PHASE = "independent-native-postflight"
    final = subprocess.run(
        [str(sys.executable), "-I", "-B", str(Path(__file__)), "--postflight"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=160,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(
        final.returncode == 0 and b"I9_MID_UV_KILL_RECOVERY_PRIVATE_PARITY=PASS" in final.stdout,
        "I9 independent full-source postflight refused",
    )
    print("I9_REAL_MID_UV_PROCESS_KILL_AND_RECOVERY=PASS")


def recover() -> None:
    global PHASE
    from serverfs_mcp.windows_v25 import storage

    native()
    prev = previous_gate()
    old, new, old_bytes, new_bytes = prev.archives()
    slot, venv, _, sid = private_state()
    check_marker(slot, "attempted.json", sid)
    check_marker(slot, "terminated-midwrite.json", sid)
    require(not (slot / "recovered.json").exists(), "I9 already recovered")
    PHASE = "real-offline-old-Wheel-reinstall-after-uv-kill"
    prev.wheel_install(venv, old, sid)
    require(prev.installed(venv, old_bytes, sid), "I9 prior package source parity not recovered")
    require(
        not prev.installed(venv, new_bytes, sid), "I9 stress candidate source remains installed"
    )
    PHASE = "disposable-only-inert-residual-cleanup"
    package = venv / "Lib" / "site-packages" / "serverfs_mcp"
    # Only I9 synthetic ballast names in the private disposable venv;
    # exact capped set, no wildcard recursive deletion, no production paths.
    residue = [package / pad_name(i) for i in range(PADDING_COUNT)]
    for item in residue:
        if item.exists():
            require(
                item.is_file() and not item.is_symlink() and item.stat().st_size <= PADDING_BYTES,
                "I9 residual ballast is not a bounded regular file",
            )
            storage.check_private(item, directory=False, sid=sid)
            item.unlink()
    require(not any(item.exists() for item in residue), "I9 inert ballast residue survived")
    marker(slot, "recovered.json", sid)
    native()
    print("I9_REAL_UV_OLD_WHEEL_RECOVERED=PASS")


def postflight() -> None:
    native()
    from serverfs_mcp.windows_v25 import storage

    prev = previous_gate()
    old, new, old_bytes, new_bytes = prev.archives()
    slot, venv, _, sid = private_state()
    for name in ("attempted.json", "terminated-midwrite.json", "recovered.json"):
        check_marker(slot, name, sid)
    check_stress_wheel(new, stress_wheel_path(slot, new), sid)
    require(prev.installed(venv, old_bytes, sid), "I9 full installed source not original Wheel")
    require(not prev.installed(venv, new_bytes, sid), "I9 updated source still installed")
    pkg = venv / "Lib" / "site-packages" / "serverfs_mcp"
    require(
        not any((pkg / pad_name(i)).exists() for i in range(PADDING_COUNT)),
        "I9 inert probe residue present",
    )
    home = storage.home("core", INSTANCE)
    storage.check_private(home, directory=True, sid=sid)
    require(not any(home.iterdir()), "I9 unexpectedly registered a Core service")
    print("I9_MID_UV_KILL_RECOVERY_PRIVATE_PARITY=PASS")
    print("I9_NO_NEW_SCHEDULER_MANIFEST=PASS")
    print("I9_I5_I7_AND_PRODUCTION_PROTECTED=PASS")


def status() -> None:
    """No writes or repeated one-use steps; report only fixed markers."""
    native()
    slot, venv, _, sid = private_state()
    for name in ("attempted.json", "terminated-midwrite.json", "recovered.json"):
        path = slot / name
        if path.exists():
            check_marker(slot, name, sid)
            state = "PRESENT"
        else:
            state = "ABSENT"
        print("I9_STATUS_" + name.upper().replace(".", "_").replace("-", "_") + "=" + state)
    old, _, old_bytes, _ = previous_gate().archives()
    installed_old = previous_gate().installed(venv, old_bytes, sid)
    print("I9_STATUS_INSTALLED_OLD_WHEEL=" + ("YES" if installed_old else "NO"))
    print("I9_STATUS_READONLY=PASS")


def main() -> None:
    global PHASE
    parser = argparse.ArgumentParser()
    exclusive = parser.add_mutually_exclusive_group(required=True)
    for name in ("audit", "stage", "exercise-once", "recover", "postflight", "status"):
        exclusive.add_argument("--" + name, action="store_true")
    args = parser.parse_args()
    if args.audit:
        audit()
    elif args.stage:
        stage()
    elif args.exercise_once:
        exercise_once()
    elif args.recover:
        recover()
    elif args.postflight:
        postflight()
    else:
        status()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 — no Windows private evidence leaked
        print(
            "I9_REAL_UV_INTERRUPTION=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

"""One-use recovery for a known I2 Bridge owner partial native installation.

The first create-only installer already created protected I2 environments and
a private health grant, then stopped before service manifest publication.
This script NEVER runs that installer again. It accepts only that exact state,
rebuilds/reinstalls the one changed owner wheel OFFLINE, verifies all installed
package code, and registers only the new i2-smoke task DISABLED.

On any failure: do not rerun or clean; preserve artifacts for read-only audit.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
import zipfile
from pathlib import Path, PurePosixPath

INSTANCE = "i2-smoke"
REPO = Path(__file__).resolve().parents[2]
PHASE = "entry"


class I2ResumeError(RuntimeError):
    """Fail-closed I2 one-use recovery refusal."""


def require(value: bool, message: str) -> None:
    if not value:
        raise I2ResumeError(message)


def load_script(filename: str, name: str):
    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, source)
    require(spec is not None and spec.loader is not None, "reviewed I2 source is absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(module.INSTANCE == INSTANCE, "I2 instance identity differs")
    return module


def verify_package_wheel(
    wheel: Path, site_packages: Path | None, sid: str, storage, source_root: Path | None = None
) -> None:
    """Compare every packaged Python module to exact installed and/or source bytes.

    A wheel may include metadata, but no private config or credential state;
    this function opens only .py members beneath the approved package root.
    """
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

    _no_reparse_components(wheel)
    storage.check_private(wheel, directory=False, sid=sid)
    count = 0
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if not name.startswith("serverfs_mcp/") or not name.endswith(".py"):
                continue
            parts = PurePosixPath(name).parts
            require(
                len(parts) >= 2 and parts[0] == "serverfs_mcp" and ".." not in parts,
                "owner package contains an unsafe module",
            )
            code = archive.read(name)
            count += 1
            for base in (site_packages, source_root):
                if base is None:
                    continue
                current = base.joinpath(*parts)
                _no_reparse_components(current)
                require(current.is_file() and not current.is_symlink(), "owner module is absent")
                if base == site_packages:
                    storage.check_private(current, directory=False, sid=sid)
                with current.open("rb") as handle:
                    require(handle.read() == code, "owner package module bytes differ")
    require(count > 0, "owner package contains no Python modules")


def main() -> None:
    global PHASE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.cli import _assert_instance_isolation
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    PHASE = "native-owner"
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I2 recovery requires a non-elevated native Windows Python -I -B",
    )
    sid = current_user_sid()
    gate = load_script("v0250-i2-native-preflight.py", "i2_resume_readonly_gate")
    stage = load_script("v0250-i2-stage.py", "i2_resume_original_stage")
    audit = load_script("v0250-i2-postfail-readonly.py", "i2_resume_code_audit")
    before = gate._scheduler_inventory()
    gate.verify_scheduler_rows(before)
    print("I2_RESUME_PRODUCTION_PREFLIGHT=EXPECTED")

    base = storage.root(INSTANCE)
    owner = storage.home("bridge", INSTANCE)
    venv = base / "envs" / "bridge"
    owner_python = venv / "Scripts" / "python.exe"
    owner_pythonw = venv / "Scripts" / "pythonw.exe"
    worker_python = base / "envs" / "agent-bridge" / "Scripts" / "python.exe"
    config = owner / "bridge.json"
    grant = owner / "state" / "core-grants.json"
    old_wheel_dir = base / "build" / "core-owner"
    old_wheels = tuple(old_wheel_dir.glob("serverfs_mcp-*.whl"))
    require(
        len(old_wheels) == 1
        and audit.classify_original_wheel(storage, base, REPO, sid=sid) == "BRIDGE_OWNER_ONLY",
        "I2 original wheel source delta differs from approved single module",
    )
    require(
        audit.classify_installed_code(
            storage,
            venv / "Lib" / "site-packages" / "serverfs_mcp" / "windows_v25" / "bridge_v2_owner.py",
            REPO / "src" / "serverfs_mcp" / "windows_v25" / "bridge_v2_owner.py",
            sid=sid,
        )
        == "DIFFERS",
        "I2 installed owner is not the known older code",
    )
    PHASE = "protected-existing-instance"
    for folder in (base, venv, owner, owner / "state", owner / "locks", old_wheel_dir):
        storage.check_private(folder, directory=True, sid=sid)
    for file in (owner_python, owner_pythonw, worker_python, config, grant):
        storage.check_private(file, directory=False, sid=sid)
    for target in (
        owner / "service.json",
        owner / "task.xml",
        base / "build" / "core-owner-resume",
    ):
        require(not target.exists() and not target.is_symlink(), "I2 resume target is occupied")
    require(not any((owner / "locks").iterdir()), "I2 Bridge owner already has lease artifacts")
    gate.verify_native_wheel(
        REPO / "native" / "windows" / "target" / "wheels" / gate.NATIVE_WHEEL_NAME
    )
    gate.verify_offline_builder()
    verify_package_wheel(old_wheels[0], venv / "Lib" / "site-packages", sid, storage)
    manifest = Manifest.parse(
        {
            "schema": 2,
            "instance_id": INSTANCE,
            "component": "bridge",
            "sid": sid,
            "pythonw": str(owner_pythonw),
            "settings": {"config": str(config), "bridge_python": str(worker_python)},
        }
    )
    load_native_bridge_plan(manifest)
    _assert_instance_isolation(manifest)
    print("I2_RESUME_ORIGINAL_STATE=VERIFIED")

    # Only after ALL read-only and original-artifact checks may one new
    # build directory be created inside this isolated, protected instance.
    PHASE = "offline-owner-wheel-refresh"
    rebuilt = base / "build" / "core-owner-resume"
    stage._new_dir(rebuilt, sid)
    wheel = stage._wheel(
        REPO,
        rebuilt,
        "serverfs_mcp-*.whl",
        (
            "windows_v25/runtime.py",
            "windows_v25/bridge_v2_owner.py",
            "windows_v25/cli.py",
            "windows_v25/storage.py",
            "agent_core_grant_client.py",
        ),
    )
    stage._protect_tree(rebuilt, sid)
    verify_package_wheel(wheel, None, sid, storage, source_root=REPO / "src")
    print("I2_RESUME_REFRESH_WHEEL=VERIFIED")

    PHASE = "offline-owner-reinstall"
    env = dict(os.environ)
    env["UV_LINK_MODE"] = "copy"
    stage.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--reinstall",
            "--no-deps",
            "--python",
            str(owner_python),
            str(wheel),
        ],
        env=env,
        timeout=180,
    )
    stage._protect_tree(venv, sid)
    verify_package_wheel(wheel, venv / "Lib" / "site-packages", sid, storage)
    stage.verify_installed_runtime(base, owner_python, worker_python)
    print("I2_RESUME_INSTALLED_OWNER=VERIFIED")

    PHASE = "independent-registration-preflight"
    load_native_bridge_plan(manifest)
    _assert_instance_isolation(manifest)
    gate.verify_scheduler_rows(gate._scheduler_inventory())
    require(
        not (owner / "service.json").exists()
        and not (owner / "service.json").is_symlink()
        and not (owner / "task.xml").exists()
        and not (owner / "task.xml").is_symlink(),
        "I2 manifest or Task XML was unexpectedly created",
    )
    PHASE = "private-service-manifest"
    storage.stage(manifest)
    storage.load("bridge", INSTANCE)
    print("I2_RESUME_SERVICE_MANIFEST=PRIVATE")

    PHASE = "new-disabled-task-registration"
    installed = Scheduler().install(manifest)
    require(
        installed.get("registered") is True and installed.get("enabled") is False,
        "I2 task not registered Disabled",
    )
    status = Scheduler().status(manifest)
    require(
        status.get("state") == "Disabled" and not status.get("running"), "I2 task state drifted"
    )
    after = gate._scheduler_inventory()
    gate.verify_scheduler_rows([row for row in after if row.get("name") in gate.PRODUCTION])
    require(
        len(after) == len(before) + 1
        and any(
            row.get("name") == gate.TASK
            and row.get("state") == "Disabled"
            and row.get("enabled") is False
            for row in after
        ),
        "I2 exact post-install task proof is unavailable",
    )
    print("I2_RESUME_PRODUCTION_TASKS=UNCHANGED")
    print("I2_RESUME_BRIDGE_TASK=REGISTERED_DISABLED")
    print("I2_NATIVE_STAGE_RECOVERED=PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - sanitized, nonsecret markers only
        print(
            "I2_RESUME=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

"""One-use offline wheel repair of ONLY i5-shared's stopped Bridge owner venv.

I5 native installed owner lacks windows_v25.legacy_bridge_ownership; its source
has been added after the original wheel was built. Never copy source modules
by hand into site-packages or touch production/I1-I4. Build and verify one
new private wheel, dry-run and reinstall this single package into the exact
DISABLED I5 Bridge owner environment, then re-protect/verify installed bytes.

Do not rerun after any failure: the fresh recovery directory is a one-use
journal and old wheel remains in i5's existing private build directory.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
import zipfile
from pathlib import Path

PHASE = "entry"
INSTANCE = "i5-shared"
CHECKS = (
    "windows_v25/legacy_bridge_ownership.py",
    "windows_v25/runtime.py",
    "windows_v25/cli.py",
    "windows_v25/bridge_v2_owner.py",
    "windows_v25/workdir_ownership.py",
)


class I5BridgeWheelRepairError(RuntimeError):
    """Sanitized refusal; never include protected file/command content."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5BridgeWheelRepairError(reason)


def _helper(filename: str, name: str):
    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, source)
    require(spec is not None and spec.loader is not None, "I5 fixed helper missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _task_baseline(gate, manifest) -> None:
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    gate.protected_tasks()
    gate.verify_candidates(gate.candidates(), installed=True)
    status = Scheduler().status(manifest)
    require(
        status.get("registered") is True
        and status.get("state") == "Disabled"
        and status.get("enabled") is False
        and status.get("running") is False
        and status.get("readiness") == "offline"
        and _current_identity(manifest) is None,
        "I5 Bridge task is not registered Disabled/offline",
    )


def main() -> None:
    global PHASE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native non-elevated Windows Python -I -B required",
    )
    repo = Path(__file__).resolve().parents[2]
    gate = _helper("v0250-i5-provider-shared-preflight.py", "i5_repair_host_gate")
    installer = _helper("v0250-i2-stage.py", "i5_repair_offline_wheels")
    require(installer.REPO == repo and gate.BRIDGE == INSTANCE, "I5 repair source mismatch")
    manifest = storage.load("bridge", INSTANCE)
    sid = current_user_sid()
    root = storage.root(INSTANCE)
    venv = root / "envs" / "bridge"
    python = venv / "Scripts" / "python.exe"
    storage.check_private(venv, directory=True, sid=sid)
    storage.check_private(python, directory=False, sid=sid)

    PHASE = "protected-and-offline-inventory"
    _task_baseline(gate, manifest)
    print("I5_BRIDGE_REPAIR_TASKS=DISABLED_PROTECTED")

    PHASE = "exact-old-installed-module-absence"
    old_source = (
        "import importlib.util\n"
        "assert importlib.util.find_spec("
        "'serverfs_mcp.windows_v25.legacy_bridge_ownership') is None\n"
        "import serverfs_mcp.windows_v25.runtime\n"
        "print('I5_OLD_OWNER_MODULE_ABSENT')\n"
    )
    evidence = installer.run([str(python), "-I", "-B", "-c", old_source], timeout=30)
    require(
        evidence.strip() == b"I5_OLD_OWNER_MODULE_ABSENT",
        "I5 Bridge owner package is not the known missing-module snapshot",
    )
    print("I5_BRIDGE_REPAIR_OLD_MODULE=ABSENT_CONFIRMED")

    PHASE = "fresh-offline-source-wheel"
    out = root / "build" / "bridge-owner-repair-1"
    installer._new_dir(out, sid)
    wheel = installer._wheel(repo, out, "serverfs_mcp-*.whl", CHECKS)
    installer._protect_tree(out, sid)
    storage.check_private(wheel, directory=False, sid=sid)
    print("I5_BRIDGE_REPAIR_WHEEL=SOURCE_VERIFIED")

    PHASE = "dry-run-private-package-upgrade"
    args = [
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
    env = dict(os.environ)
    env["UV_LINK_MODE"] = "copy"
    installer.run([*args, "--dry-run"], env=env, timeout=60)
    _task_baseline(gate, manifest)
    print("I5_BRIDGE_REPAIR_DRY_RUN=PASS")

    PHASE = "oneuse-private-package-reinstall"
    installer.run(args, env=env, timeout=120)
    installer._protect_tree(venv, sid)

    PHASE = "verify-exact-installed-wheel-and-private-dacl"
    package = venv / "Lib" / "site-packages" / "serverfs_mcp"
    with zipfile.ZipFile(wheel) as archive:
        for relative in CHECKS:
            expected = archive.read("serverfs_mcp/" + relative)
            installed = package / relative
            storage.check_private(installed, directory=False, sid=sid)
            require(
                installed.read_bytes() == expected,
                "I5 installed owner package differs from verified private wheel",
            )
    evidence = installer.run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            "from serverfs_mcp.windows_v25.legacy_bridge_ownership "
            "import assert_legacy_writable_roots_disjoint; "
            "from serverfs_mcp.windows_v25.runtime "
            "import run_independent_bridge_v2; "
            "print('I5_BRIDGE_MODULE_IMPORTS_OK')",
        ],
        timeout=30,
    )
    require(
        evidence.strip() == b"I5_BRIDGE_MODULE_IMPORTS_OK",
        "I5 repaired private Bridge owner imports are not available",
    )
    _task_baseline(gate, manifest)
    print("I5_BRIDGE_REPAIR_INSTALLED_MODULES=PRIVATE_VERIFIED")
    print("I5_BRIDGE_REPAIR_PRODUCTION_AND_I1_I4=UNCHANGED")
    print("I5_BRIDGE_REPAIR_OFFLINE=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - redacted terminal stage/type only
        print(
            "I5_BRIDGE_REPAIR=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

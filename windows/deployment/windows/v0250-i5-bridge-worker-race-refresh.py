"""I5 worker-only offline package refresh for the Windows SQLite WAL/SHM race fix.

The installed i5-shared Agent Bridge *worker* is a disposable, Disabled
acceptance fixture, not the production Bridge. This is a one-use migration:
--audit is read-only; --stage may replace only its private worker package.
No Scheduler lifecycle calls, provider/model, public Tunnel, network, Core
venv, production service or credential reads are permitted.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTANCE = "i5-shared"
CHECKS = (
    "store.py",
    "private_state.py",
    "core_grants_private.py",
    "protocol.py",
    "service.py",
    "adapters/claude.py",
)
PHASE = "entry"


class I5WorkerRefreshError(RuntimeError):
    """Fixed redacted refusal; no private paths or credentials in output."""


def require(value: bool, reason: str) -> None:
    if not value:
        raise I5WorkerRefreshError(reason)


def helper(filename: str, name: str):
    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, source)
    require(spec is not None and spec.loader is not None, "fixed helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _host() -> None:
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native non-elevated Windows Python -I -B required",
    )


def _baseline(gate, named, storage):
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    gate.protected_tasks()
    gate.verify_candidates(gate.candidates(), installed=True)
    named.fixture_tasks(installed=True)
    sid = current_user_sid()
    manifest = storage.load("bridge", INSTANCE)
    require(manifest.instance_id == INSTANCE, "isolated Bridge identity mismatches")
    service = Scheduler()
    for component, instance in (
        ("bridge", INSTANCE),
        ("core", "i5-core-a"),
        ("core", "i5-core-b"),
    ):
        item = storage.load(component, instance)
        state = service.status(item)
        require(
            state.get("registered") is True
            and state.get("state") == "Disabled"
            and state.get("enabled") is False
            and state.get("running") is False
            and state.get("readiness") == "offline"
            and _current_identity(item) is None,
            "isolated I5 fixture is not completely Disabled/offline",
        )
    root = storage.root(INSTANCE)
    venv = root / "envs" / "agent-bridge"
    interpreter = venv / "Scripts" / "python.exe"
    require(
        Path(str(manifest.settings["bridge_python"])).resolve(strict=True)
        == interpreter.resolve(strict=True),
        "I5 installed Bridge Worker interpreter pin differs",
    )
    package = venv / "Lib" / "site-packages" / "serverfs_agent_bridge"
    storage.check_private(venv, directory=True, sid=sid)
    storage.check_private(interpreter, directory=False, sid=sid)
    for relative in CHECKS:
        storage.check_private(package / relative, directory=False, sid=sid)
    # Retained original wheel is the rollback material; never overwrite it.
    retained = tuple((root / "build" / "bridge-worker").glob("serverfs_agent_bridge-*.whl"))
    require(len(retained) == 1, "original I5 worker wheel is not uniquely retained")
    storage.check_private(retained[0], directory=False, sid=sid)
    return root, venv, interpreter, package, sid, retained[0]


def _archive_matches(wheel: Path, package: Path | None) -> bool:
    with zipfile.ZipFile(wheel) as archive:
        require(archive.testzip() is None, "I5 offline worker wheel is invalid")
        for part in CHECKS:
            source = (
                package / part
                if package is not None
                else REPO / "agent_bridge" / "src" / "serverfs_agent_bridge" / part
            )
            if archive.read("serverfs_agent_bridge/" + part) != source.read_bytes():
                return False
        return True


def _assert_source_parity(package: Path, sid: str) -> None:
    from serverfs_mcp.windows_v25 import storage

    for part in CHECKS:
        installed = package / part
        storage.check_private(installed, directory=False, sid=sid)
        require(
            installed.read_bytes()
            == (REPO / "agent_bridge" / "src" / "serverfs_agent_bridge" / part).read_bytes(),
            "I5 worker installed package differs from checked source",
        )


def main(argv: list[str] | None = None) -> None:
    global PHASE
    arguments = argparse.ArgumentParser()
    action = arguments.add_mutually_exclusive_group(required=True)
    action.add_argument("--audit", action="store_true")
    action.add_argument("--stage", action="store_true")
    action.add_argument("--postflight", action="store_true")
    args = arguments.parse_args(argv)

    _host()
    from serverfs_mcp.windows_v25 import storage

    gate = helper("v0250-i5-provider-shared-preflight.py", "i5_worker_refresh_gate")
    named = helper("v0250-i6-named-connectors-stage.py", "i5_worker_named_protection")
    require(
        gate.BRIDGE == INSTANCE and named.IDS == ("i6-edge-a", "i6-edge-b"),
        "protected source identities changed",
    )
    PHASE = "read-only-protected-baseline"
    root, venv, interpreter, package, sid, old_wheel = _baseline(gate, named, storage)
    print("I5_WORKER_REFRESH_PROTECTED_AND_I6_DISABLED=PASS")

    target = root / "build" / "bridge-worker-race-refresh-1"
    if args.audit:
        require(not target.exists() and not target.is_symlink(), "I5 refresh slot occupied")
        require(
            _archive_matches(old_wheel, package),
            "I5 installed worker is not the retained original wheel",
        )
        require(
            (package / "store.py").read_bytes()
            != (REPO / "agent_bridge" / "src" / "serverfs_agent_bridge" / "store.py").read_bytes(),
            "I5 installed Worker already matches the new source",
        )
        print("I5_WORKER_REFRESH_ORIGINAL_WHEEL=VERIFIED")
        print("I5_WORKER_REFRESH_CANDIDATE=PASS")
        return

    if args.stage:
        # Stage is single-use. It may never overwrite existing repair state.
        require(not target.exists() and not target.is_symlink(), "I5 refresh slot occupied")
        require(
            _archive_matches(old_wheel, package),
            "I5 original installed wheel differs from retained source",
        )
        installer = helper("v0250-i2-stage.py", "i5_worker_refresh_wheel_helpers")
        require(installer.REPO == REPO, "source wheel helper mismatch")
        PHASE = "private-one-use-build"
        installer._new_dir(target, sid)
        wheel = installer._wheel(
            REPO / "agent_bridge", target, "serverfs_agent_bridge-*.whl", CHECKS
        )
        installer._protect_tree(target, sid)
        require(_archive_matches(wheel, None), "new worker wheel differs from source")
        print("I5_WORKER_REFRESH_SOURCE_WHEEL=VERIFIED")

        PHASE = "offline-dry-run"
        cmd = [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--reinstall",
            "--python",
            str(interpreter),
            str(wheel),
        ]
        env = dict(os.environ)
        env["UV_LINK_MODE"] = "copy"
        installer.run([*cmd, "--dry-run"], env=env, timeout=60)
        _baseline(gate, named, storage)
        print("I5_WORKER_REFRESH_DRY_RUN=PASS")

        PHASE = "only-i5-worker-offline-package-reinstall"
        installer.run(cmd, env=env, timeout=120)
        installer._protect_tree(venv, sid)
        print("I5_WORKER_REFRESH_PRIVATE_INSTALL=PASS")

    PHASE = "source-parity-and-runtime-imports"
    _assert_source_parity(package, sid)
    installer = helper("v0250-i2-stage.py", "i5_worker_refresh_verify")
    imported = installer.run(
        [
            str(interpreter),
            "-I",
            "-B",
            "-c",
            "from serverfs_agent_bridge.store import TaskStore; "
            "from serverfs_agent_bridge.core_grants_private import load_private_core_grants; "
            "from serverfs_agent_bridge.adapters.claude import ClaudeAdapter; "
            "print('I5_WORKER_REFRESH_IMPORT_OK')",
        ],
        timeout=30,
    )
    require(
        imported.strip() == b"I5_WORKER_REFRESH_IMPORT_OK",
        "updated I5 worker modules cannot be isolated-imported",
    )
    _baseline(gate, named, storage)
    print("I5_WORKER_REFRESH_SOURCE_AND_PRIVATE_DACL=VERIFIED")
    print("I5_WORKER_REFRESH_INSTALLED_POSTFLIGHT=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - never expose stderr/credentials
        print(
            "I5_WORKER_REFRESH=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

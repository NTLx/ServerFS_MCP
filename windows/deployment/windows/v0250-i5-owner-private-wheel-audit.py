"""Read-only I5 shared Bridge Owner source/wheel rollback preflight.

Native user-mode Windows only. No provider execution, Scheduler transitions,
credential reads, package installation, network or production modification.
Compare installed code to the original retained private Wheel by complete
module bytes; enumerate the exact seven pending source deltas. Never assume
that a file named like a rollback Wheel actually matches the installed state.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
import zipfile
from pathlib import Path

INSTANCE = "i5-shared"
CORES = ("i5-core-a", "i5-core-b")
EXPECTED_DELTA = frozenset(
    "windows_v25/" + name + ".py"
    for name in ("cli", "isolation", "readiness", "runtime", "spec", "storage", "tasks")
)
REPO = Path(__file__).resolve().parents[2]


class I5OwnerAuditError(RuntimeError):
    """Private paths and commands are never exposed in result text."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5OwnerAuditError(reason)


def helper(name: str, label: str):
    source = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "fixed helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_modules() -> dict[str, bytes]:
    root = REPO / "src/serverfs_mcp"
    result: dict[str, bytes] = {}
    for item in root.rglob("*.py"):
        require(item.is_file() and not item.is_symlink(), "source path unsafe")
        result[item.relative_to(root).as_posix()] = item.read_bytes()
    require(len(result) >= 20 and EXPECTED_DELTA <= result.keys(), "source inventory invalid")
    return result


def installed_modules(pkg: Path, modules: dict[str, bytes], sid: str) -> dict[str, bytes]:
    from serverfs_mcp.windows_v25 import storage

    storage.check_private(pkg, directory=True, sid=sid)
    files: dict[str, bytes] = {}
    for name in modules:
        item = pkg / name
        require(item.is_file() and not item.is_symlink(), "installed module missing/unsafe")
        storage.check_private(item, directory=False, sid=sid)
        files[name] = item.read_bytes()
    return files


def wheel_modules(wheel: Path, names: set[str]) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as source:
        require(source.testzip() is None, "old private Wheel ZIP corrupt")
        return {name: source.read("serverfs_mcp/" + name) for name in names}


def old_private_wheels(base: Path, installed: dict[str, bytes], sid: str) -> tuple[Path, ...]:
    from serverfs_mcp.windows_v25 import storage

    build = base / "build"
    storage.check_private(build, directory=True, sid=sid)
    matches: list[Path] = []
    candidates = tuple(build.rglob("serverfs_mcp-*.whl"))
    require(0 < len(candidates) <= 24, "old private Wheel candidates invalid")
    for wheel in candidates:
        require(wheel.is_file() and not wheel.is_symlink(), "private Wheel path unsafe")
        storage.check_private(wheel, directory=False, sid=sid)
        try:
            if wheel_modules(wheel, set(installed)) == installed:
                matches.append(wheel)
        except (KeyError, ValueError, zipfile.BadZipFile):
            # A different private historical build is not a rollback match.
            continue
    require(bool(matches), "no retained private Wheel reproduces installed owner")
    return tuple(matches)


def native_preflight() -> tuple[Path, str, dict[str, bytes], dict[str, bytes], tuple[Path, ...]]:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "requires native unprivileged Windows -I -B",
    )
    i7 = helper("v0250-i7-connectors-disabled-register.py", "i5_audit_i7_protected")
    i7.postflight()
    i5 = helper("v0250-i5-provider-shared-preflight.py", "i5_audit_identity")
    require(i5.BRIDGE == INSTANCE and set(i5.CORE_PORTS) == set(CORES), "I5 identity drift")
    i5.protected_tasks()
    i5.verify_candidates(i5.candidates(), installed=True)
    sid = current_user_sid()
    require(sid == storage._sid(), "private Windows SID changed")
    scheduler = Scheduler()
    for component, instance in (("bridge", INSTANCE), *(("core", core) for core in CORES)):
        manifest = storage.load(component, instance)
        state = scheduler.status(manifest)
        require(
            state.get("registered") is True
            and state.get("state") == "Disabled"
            and state.get("enabled") is False
            and state.get("running") is False
            and _current_identity(manifest) is None,
            "I5 exact instance not Disabled/offline",
        )
    require(
        all(
            listener.port not in i5.CORE_PORTS.values()
            for listener in _windows_ipv4_tcp_listeners()
        ),
        "I5 Core port unexpectedly bound",
    )
    base = storage.root(INSTANCE)
    env = base / "envs/bridge"
    storage.check_private(env, directory=True, sid=sid)
    source = source_modules()
    actual = installed_modules(env / "Lib/site-packages/serverfs_mcp", source, sid)
    differences = {name for name, data in source.items() if actual[name] != data}
    require(differences == EXPECTED_DELTA, "I5 Bridge source changed beyond seven-module allowlist")
    old = old_private_wheels(base, actual, sid)
    print("I5_BRIDGE_OWNER_SEVEN_MODULE_DELTA=VERIFIED")
    print("I5_BRIDGE_OWNER_ROLLBACK_WHEEL_MATCHES=" + str(len(old)))
    print("I5_THREE_TASKS_DISABLED_AND_PROTECTED=PASS")
    return base, sid, source, actual, old


def main() -> None:
    native_preflight()
    print("I5_OWNER_PRIVATE_WHEEL_READONLY_AUDIT=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - print class only, not private path
        print(
            "I5_OWNER_PRIVATE_WHEEL_READONLY_AUDIT=FAILED type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None

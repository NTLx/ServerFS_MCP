"""Read-only I5 startup blocker diagnosis: legacy vs new writable Bridge roots.

This does NOT stop the active legacy task or start I5. The installed legacy
task XML must authenticate its manifest and actual native TOML workdir policy
before any disjointness conclusion. Fail closed on all missing evidence.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
from pathlib import Path


class I5LegacyOverlapError(RuntimeError):
    """Never disclose protected host files or paths in the result."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5LegacyOverlapError(reason)


def main() -> None:
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import (
        TASK_NAME,
        _manifest_document,
        _query_task_xml,
        validate_registered_task,
    )
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_owned_bridge_workdir_claims
    from serverfs_mcp.windows_v25.cli import _legacy_bridge_active
    from serverfs_mcp.windows_v25.runtime import _roots
    from serverfs_mcp.windows_v25.workdir_ownership import (
        WorkdirClaim,
        assert_independent_bridge_roots,
    )

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I5 overlap check needs native non-elevated Python -I -B",
    )
    source = Path(__file__).with_name("v0250-i5-provider-shared-preflight.py")
    spec = importlib.util.spec_from_file_location("i5_overlap_protected_gate", source)
    require(spec is not None and spec.loader is not None, "I5 protected gate absent")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    gate.protected_tasks()
    gate.verify_candidates(gate.candidates(), installed=True)
    print("I5_LEGACY_OVERLAP_PROTECTED_TASKS=EXPECTED")

    require(_legacy_bridge_active() is True, "legacy active-state blocker not reproduced")
    print("I5_LEGACY_TASK=RUNNING")

    installed = storage.load("bridge", "i5-shared")
    proposed = load_owned_bridge_workdir_claims(installed)
    require(
        len(proposed) == 1
        and proposed[0].writable is True
        and proposed[0].bridge_id == "i5-shared",
        "I5 private proposed writable workdir is not exact",
    )
    private_fixture = storage.root("i5-shared") / "workdirs" / "fixture"
    storage.check_private(private_fixture, directory=True, sid=current_user_sid())
    require(
        os.path.normcase(proposed[0].root)
        == os.path.normcase(str(private_fixture.resolve(strict=True))),
        "I5 proposed workdir is not its private fixture",
    )
    print("I5_PRIVATE_WRITABLE_FIXTURE=VERIFIED")

    legacy = _manifest_document()
    require(
        legacy.get("task_name") == TASK_NAME and legacy.get("user_sid") == current_user_sid(),
        "legacy task identity does not match current owner",
    )
    xml = _query_task_xml()
    require(isinstance(xml, str) and xml, "legacy Scheduler XML unavailable")
    valid, _reason = validate_registered_task(xml, legacy)
    require(valid, "legacy task XML and manifest are not authenticated")
    print("I5_LEGACY_REGISTERED_TASK_POLICY=VERIFIED")

    path = legacy.get("config_path")
    require(isinstance(path, str) and path, "legacy task native policy path absent")
    native_config, canonical_roots = _roots(Path(path))
    workdirs, _settings = load_native_config(native_config)
    require(
        len(workdirs) == len(canonical_roots) and bool(workdirs),
        "legacy native workdir roots cannot be established",
    )
    legacy_claims = tuple(
        WorkdirClaim("default", str(root), not workdir.read_only)
        for workdir, root in zip(workdirs, canonical_roots, strict=True)
    )
    print("I5_LEGACY_EFFECTIVE_WORKDIR_CLAIMS=VERIFIED")
    assert_independent_bridge_roots(proposed, legacy_claims)
    print("I5_I5_SHARED_AND_LEGACY_WRITABLE_ROOTS=DISJOINT")
    print("I5_LEGACY_OVERLAP_READONLY=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - only type name
        print("I5_LEGACY_OVERLAP=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None

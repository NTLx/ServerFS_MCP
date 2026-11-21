"""I5 exact stopped-Bridge owner private offline wheel repair contracts.

Tests inspect source only; no native Wheel build, pip, Scheduler or host state.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-repair-private-bridge-wheel.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_repair_private_wheel", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i5_repair_targets_only_legacy_missing_module_and_bridge_owner() -> None:
    mod = _module()
    assert mod.INSTANCE == "i5-shared"
    assert mod.CHECKS == (
        "windows_v25/legacy_bridge_ownership.py",
        "windows_v25/runtime.py",
        "windows_v25/cli.py",
        "windows_v25/bridge_v2_owner.py",
        "windows_v25/workdir_ownership.py",
    )
    with pytest.raises(mod.I5BridgeWheelRepairError):
        mod.require(False, "blocked")


def test_i5_repair_preserves_protection_then_builds_one_offline_wheel() -> None:
    source = _SOURCE.read_text(encoding="utf-8")
    for required in (
        "installer._new_dir(out, sid)",
        'installer._wheel(repo, out, "serverfs_mcp-*.whl", CHECKS)',
        "installer._protect_tree(out, sid)",
        '"--offline"',
        '"--reinstall"',
        '"--no-deps"',
        'installer.run([*args, "--dry-run"]',
        "installer.run(args, env=env, timeout=120)",
        "installer._protect_tree(venv, sid)",
        "zipfile.ZipFile(wheel)",
        "installed.read_bytes() == expected",
        "I5_BRIDGE_REPAIR_OFFLINE=PASS",
    ):
        assert required in source


def test_i5_repair_must_verify_old_snapshot_and_disabled_tasks_before_writes() -> None:
    source = _SOURCE.read_text(encoding="utf-8")
    assert source.index("_task_baseline(gate, manifest)") < source.index(
        "installer._new_dir(out, sid)"
    )
    assert source.index("I5_BRIDGE_REPAIR_OLD_MODULE=ABSENT_CONFIRMED") < source.index(
        "installer._new_dir(out, sid)"
    )
    assert source.index("I5_BRIDGE_REPAIR_DRY_RUN=PASS") < source.index(
        "installer.run(args, env=env, timeout=120)"
    )
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "storage.stage(",
        "storage.prepare(",
        "uv pip uninstall",
        "shutil.rmtree(",
        "os.system(",
        "git push",
        "git commit",
        "ANTHROPIC_API_KEY",
        "secret_hex",
    ):
        assert forbidden not in source

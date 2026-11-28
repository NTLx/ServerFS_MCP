"""Source contracts for an actual isolated Windows uv rollback after process death.

No real install is performed by pytest; the installed native host test uses
the separate one-use stage and subprocess crash/recover/postflight commands.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i8-real-wheel-crash-recovery.py"
)
spec = importlib.util.spec_from_file_location("i8_native_recovery_contract", SCRIPT)
assert spec is not None and spec.loader is not None
rollback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollback)


def test_private_disposable_identity_and_forced_exit_code() -> None:
    assert rollback.INSTANCE == "i8-wheel-recovery"
    assert rollback.SLOT == "real-uv-crash-recovery-1"
    assert rollback.CRASH_CODE == 79
    assert rollback.CORRUPT.startswith(b"# I8 intentionally corrupted")


def test_source_never_changes_production_or_scheduler_resources() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    required = (
        'storage.prepare("core", INSTANCE)',
        'root / "envs/core/Scripts/python.exe"',
        'storage.root("i5-shared") / "build"',
        "gate.verify_archive_delta(wheel, new, allowed=i5.EXPECTED_DELTA)",
        "i5.postflight()",
        '"--offline"',
        '"--no-deps"',
        '"UV_LINK_MODE"] = "copy"',
        'target = venv / "Lib/site-packages/serverfs_mcp/windows_v25/tasks.py"',
        "os._exit(CRASH_CODE)",
        "child.returncode == CRASH_CODE",
        '"--recover"],',
        '"--postflight"],',
        'mark(slot, "recovered.json", sid)',
        "installed(venv, old, sid)",
        "I8_REAL_OFFLINE_UV_CRASH_AND_RECOVERY=PASS",
    )
    for marker in required:
        assert marker in source, marker

    forbidden = (
        "Scheduler().install(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().uninstall(",
        "storage.stage(",
        'storage.load("cloudflare"',
        "ANTHROPIC_API_KEY",
        "tunnel.token",
        "cloudflare.request(",
        "http.env",
        "git push",
        "shutil.rmtree(",
        "TerminateProcess",
        "task.submit",
    )
    for marker in forbidden:
        assert marker not in source, marker


def test_real_uv_command_arguments_are_semantic_and_offline() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    install = funcs["wheel_install"]
    calls = [
        node
        for node in ast.walk(install)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and node.args
        and isinstance(node.args[0], ast.List)
    ]
    assert len(calls) == 1
    argv = calls[0].args[0]
    values = [item.value for item in argv.elts if isinstance(item, ast.Constant)]
    assert values == ["uv", "pip", "install", "--offline", "--no-deps", "--reinstall", "--python"]
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and any(isinstance(x, ast.Constant) and x.value == "3.12.10" for x in ast.walk(node))
        for node in ast.walk(funcs["stage"])
    )


def test_all_rollback_writes_remain_scoped_to_i8() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "root, slot, venv, py = paths()" in source
    assert "storage.root(INSTANCE)" in source
    assert 'storage.home("core", INSTANCE)' in source
    assert "target.write_bytes(CORRUPT)" in source
    assert 'storage.root("i5-shared") / "build"' in source
    assert "I8 real uv" not in source  # no hard-coded output masquerading as a verified run


def test_one_use_flags_and_sanitized_error_channel() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for name in (
        '"--audit"',
        '"--stage"',
        '"--exercise-once"',
        '"--crash-child"',
        '"--recover"',
        '"--postflight"',
        '"crash-intent.json"',
        '"crash-applied.json"',
        '"recovered.json"',
        "type(exc).__name__",
    ):
        assert name in source
    for msg in ("not", "already", "test-only"):
        with pytest.raises(rollback.I8RecoveryRefusal):
            rollback.require(False, msg)
    rollback.require(True, "not-a-error")

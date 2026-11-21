"""Source-only contract for I9 genuine mid-uv kill and I9-only recovery.

These tests must not create a virtual environment, launch uv, or alter any
Windows Scheduler/Cloudflare/production state.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i9-real-mid-uv-interruption.py"
)
SOURCE = SCRIPT.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FUNCTIONS = {node.name: node for node in TREE.body if isinstance(node, ast.FunctionDef)}


def load():
    spec = importlib.util.spec_from_file_location("i9_mid_uv_isolation", SCRIPT)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_disposable_only_instance_and_distinct_oneuse_markers() -> None:
    mod = load()
    assert mod.INSTANCE == "i9-uv-midkill"
    assert mod.INSTANCE != "i8-wheel-recovery"
    assert mod.SLOT == "mid-uv-once"
    assert mod.MARKERS == {"prepared", "attempted", "killed", "completed", "recovered"}
    for mode in ("audit", "stage", "exercise-once", "recover", "postflight"):
        assert '"--" + name' in SOURCE
        assert mode in SOURCE


def test_actual_uv_popen_offline_command_targets_only_i9_venv() -> None:
    fn = FUNCTIONS["exercise_once"]
    spawns = [
        item
        for item in ast.walk(fn)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "Popen"
    ]
    assert len(spawns) == 1
    argv = spawns[0].args[0]
    assert isinstance(argv, ast.List)
    const = [item.value for item in argv.elts if isinstance(item, ast.Constant)]
    assert const == ["uv", "pip", "install", "--offline", "--no-deps", "--reinstall", "--python"]
    keywords = {keyword.arg for keyword in spawns[0].keywords}
    assert {"cwd", "env", "stdin", "stdout", "stderr", "creationflags"} <= keywords
    assert "shell" not in keywords
    assert "cwd=str(venv)" in SOURCE
    assert "UV_LINK_MODE" in SOURCE and "UV_NO_CACHE" in SOURCE


def test_only_child_is_killed_after_real_uv_module_disappearance() -> None:
    source = ast.get_source_segment(SOURCE, FUNCTIONS["exercise_once"])
    assert source is not None
    assert 'target = venv / "Lib/site-packages/serverfs_mcp/windows_v25/tasks.py"' in source
    assert "if not target.exists() and process.poll() is None:" in source
    assert "process.kill()" in source
    assert "killed = process.returncode != 0" in source
    assert 'marker(slot, "killed", sid, create=True)' in source
    assert 'marker(slot, "completed", sid, create=True)' in source
    assert source.index('marker(slot, "attempted", sid, create=True)') < source.index(
        "subprocess.Popen("
    )
    assert "require(killed, " in source
    assert source.index("require(killed, ") < source.index(
        '"I9_REAL_UV_MID_TRANSACTION_INTERRUPT_E2E=PASS"'
    )


def test_separate_real_uv_recovery_and_postflight_prove_old_bytes() -> None:
    for expected in (
        "i8().archives()",
        "wheel.wheel_install(venv, oldwheel, sid)",
        "wheel.installed(venv, old, sid)",
        "not wheel.installed(venv, new, sid)",
        '["uv", "venv", "--offline", "--python", "3.12.10", str(venv)]',
        '"--recover"',
        '"--postflight"',
        'not (home / "service.json").exists()',
        "not any(home.iterdir())",
        "I9_PRODUCTION_AND_I5_I7_PROTECTED=PASS",
        "I9_PRIVATE_BYTE_EXACT_OLD_WHEEL_AND_DACL=PASS",
    ):
        assert expected in SOURCE, expected
    execute = ast.get_source_segment(SOURCE, FUNCTIONS["exercise_once"])
    assert execute is not None
    assert execute.count("subprocess.run(") == 2
    assert "rollback.returncode == 0" in execute
    assert "post.returncode == 0" in execute


def test_no_non_i9_process_or_install_and_no_dummy_corruption() -> None:
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().uninstall(",
        "storage.stage(",
        'storage.root("i5-shared")',
        'storage.root("i7-public")',
        "TerminateProcess(",
        "taskkill",
        "os.kill(",
        "os._exit(",
        "shutil.rmtree(",
        "cloudflare.request(",
        "tunnel.token",
        "ANTHROPIC_API_KEY",
        "repo.commit",
        "git push",
        "target.write_bytes(",
        "shell=True",
    ):
        assert forbidden not in SOURCE, forbidden


def test_oneuse_and_inconclusive_remain_distinct_from_accepted_real_kill() -> None:
    for expected in (
        "not marker(slot, key, sid) for key in",
        '"I9_REAL_UV_INTERRUPTION=INCONCLUSIVE"',
        '"I9_REAL_UV_KILLED_WHILE_OLD_MODULE_MISSING=PASS"',
        '"I9_REAL_UV_MID_TRANSACTION_INTERRUPT_E2E=PASS"',
        '"I9_REAL_MID_UV_WINDOW="',
        "I9_NATIVE_MID_UV=FAILED phase=",
        "type(exc).__name__",
    ):
        assert expected in SOURCE, expected
    mod = load()
    with pytest.raises(mod.I9Refusal):
        mod.require(False, "untrusted state")
    mod.require(True, "expected")

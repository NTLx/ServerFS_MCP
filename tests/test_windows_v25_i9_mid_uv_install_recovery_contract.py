"""Pure non-mutating contracts for I9 actual Windows uv mid-write termination.

No process spawn, actual wheel staging, model calls, Scheduler or production writes.
The real acceptance requires separate Windows --stage and --exercise-once calls.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deployment/windows/v0250-i9-mid-uv-install-recovery.py"


def module():
    spec = importlib.util.spec_from_file_location("i9_native_midwrite_source", SCRIPT)
    assert spec and spec.loader
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def test_new_disposable_instance_and_bounded_stress_distribution() -> None:
    i9 = module()
    assert i9.INSTANCE == "i9-uv-midwrite"
    assert i9.SLOT == "real-uv-midwrite-1"
    assert i9.PADDING_COUNT == 384
    assert i9.PADDING_BYTES == 256 * 1024
    assert i9.MIN_ACTIVE_WRITE_BYTES == 8 * 1024 * 1024
    assert i9.UV_TERMINATE_CODE == 197
    assert i9.pad_name(0) == "serverfs_mcp/_i9_uv_padding_0000.bin"
    assert i9.pad_name(i9.PADDING_COUNT - 1) == "serverfs_mcp/_i9_uv_padding_0383.bin"
    for bad in (-1, i9.PADDING_COUNT):
        with pytest.raises(i9.I9MidwriteError):
            i9.pad_name(bad)


def test_record_uses_wheel_standard_sha256_urlsafe_base64_digest() -> None:
    i9 = module()
    value = b"isolated Windows I9 only"
    row = i9._record_line("serverfs_mcp/_i9_uv_padding_0000.bin", value)
    assert row[0].startswith("serverfs_mcp/_i9_uv_padding_")
    assert row[1] == "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(
        b"="
    ).decode("ascii")
    assert row[2] == str(len(value))


def test_exact_offline_native_uv_owned_process_interruption_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    funcs = {item.name: item for item in tree.body if isinstance(item, ast.FunctionDef)}
    for name in (
        "audit",
        "stage",
        "exercise_once",
        "recover",
        "postflight",
        "status",
        "terminate_only_owned_uv",
        "write_bytes_from_owned_handle",
    ):
        assert name in funcs
    execute = ast.get_source_segment(source, funcs["exercise_once"])
    assert execute is not None
    for required in (
        'marker(slot, "attempted.json", sid)',
        '"uv"',
        '"--offline"',
        '"--no-deps"',
        '"--reinstall"',
        '"--python"',
        "str(py)",
        "str(wheel)",
        'env.update(UV_LINK_MODE="copy", UV_NO_CACHE="1")',
        "stdin=subprocess.DEVNULL",
        "stdout=subprocess.DEVNULL",
        "stderr=subprocess.DEVNULL",
        "process.poll() is None",
        "write_bytes_from_owned_handle(int(process._handle))",
        "written >= MIN_ACTIVE_WRITE_BYTES",
        "terminate_only_owned_uv(process)",
        'marker(slot, "terminated-midwrite.json", sid)',
        "subprocess.run(",
        '"--recover"',
        '"--postflight"',
        '"I9_REAL_MID_UV_PROCESS_KILL_AND_RECOVERY=PASS"',
    ):
        assert required in execute, required
    launch = [
        node
        for node in ast.walk(funcs["exercise_once"])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "Popen"
    ]
    assert len(launch) == 1 and isinstance(launch[0].args[0], ast.List)
    values = [item.value for item in launch[0].args[0].elts if isinstance(item, ast.Constant)]
    assert values == ["uv", "pip", "install", "--offline", "--no-deps", "--reinstall", "--python"]
    terminate = ast.get_source_segment(source, funcs["terminate_only_owned_uv"])
    assert terminate is not None
    assert "kernel.TerminateProcess" in terminate
    assert "process._handle" in terminate
    assert "process.wait(timeout=20) == UV_TERMINATE_CODE" in terminate


def test_recovery_only_disposable_venv_and_no_registrations() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for expected in (
        'storage.prepare("core", INSTANCE)',
        "storage.root(INSTANCE)",
        'storage.home("core", INSTANCE)',
        "previous_gate().native_protected()",
        'check_marker(slot, "terminated-midwrite.json", sid)',
        "prev.wheel_install(venv, old, sid)",
        "prev.installed(venv, old_bytes, sid)",
        "check_stress_wheel(new, stress_wheel_path(slot, new), sid)",
        "item.unlink()",
        'marker(slot, "recovered.json", sid)',
        '"I9_MID_UV_KILL_RECOVERY_PRIVATE_PARITY=PASS"',
        "storage.check_private(item, directory=False, sid=sid)",
    ):
        # Some requirements occur via dynamically loaded I8, not as literal.
        if expected == "previous_gate().native_protected()":
            assert "previous_gate().native_protected()" in source
        else:
            assert expected in source, expected
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().uninstall(",
        "taskkill",
        "psutil.process_iter",
        'subprocess.Popen(["powershell.exe"',
        "ANTHROPIC_API_KEY",
        "http.env",
        "cloudflare.request(",
        "git commit",
        "git push",
        "shutil.rmtree(",
        "shell=True",
    ):
        assert forbidden not in source


def test_fail_closed_and_oneuse_durable_evidence() -> None:
    i9 = module()
    with pytest.raises(i9.I9MidwriteError):
        i9.require(False, "unsafe")
    i9.require(True, "safe")
    source = SCRIPT.read_text("utf-8")
    for flag in ("audit", "stage", "exercise-once", "recover", "postflight", "status"):
        assert f'"{flag}"' in source
    assert 'exclusive.add_argument("--" + name, action="store_true")' in source
    assert 'check_marker(slot, "attempted.json", sid)' in source
    assert 'check_marker(slot, "terminated-midwrite.json", sid)' in source
    assert 'marker(slot, "recovered.json", sid)' in source
    assert 'for name in ("attempted.json", "terminated-midwrite.json", "recovered.json"):' in source
    assert "check_marker(slot, name, sid)" in source

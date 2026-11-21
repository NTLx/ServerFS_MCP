"""Pure I2 isolated Bridge preflight contract tests; no Scheduler calls."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment" / "windows" / "v0250-i2-native-preflight.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("serverfs_i2_native_preflight", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(module):
    return [
        {
            "name": name,
            "state": state if state is not None else "Disabled",
            "enabled": enabled,
        }
        for name, (enabled, state) in module.PRODUCTION.items()
    ]


def test_i2_preflight_accepts_only_fresh_isolated_bridge_identity() -> None:
    module = _module()
    assert module.INSTANCE == "i2-smoke"
    assert module.TASK == "ServerFS v25 i2-smoke Agent Bridge"
    assert "ServerFS v25 i2-smoke Core" in module.TARGETS
    assert "Get-ScheduledTask" in module.SCRIPT
    assert "Start-ScheduledTask" not in module.SCRIPT
    assert "Register-ScheduledTask" not in module.SCRIPT
    assert "Stop-ScheduledTask" not in module.SCRIPT
    assert "Set-Acl" not in module.SCRIPT
    assert module.verify_scheduler_rows(_rows(module)) is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: rows.pop(),
        lambda rows: rows.append(dict(rows[0])),
        lambda rows: rows[0].update(enabled=False),
        lambda rows: rows[0].update(state="Ready"),
        lambda rows: rows[1].update(state="Disabled"),
        lambda rows: rows[3].update(state="Running"),
        lambda rows: rows[3].update(state="Ready"),
        lambda rows: rows[4].update(state="Ready"),
        lambda rows: rows[4].update(state="Queued"),
        lambda rows: rows[2].update(enabled="true"),
        lambda rows: rows.append(
            {
                "name": "ServerFS v25 i2-smoke Agent Bridge",
                "state": "Disabled",
                "enabled": False,
            }
        ),
    ],
)
def test_i2_preflight_refuses_occupied_or_changed_state(mutation) -> None:
    module = _module()
    rows = _rows(module)
    mutation(rows)
    with pytest.raises(module.I2PreflightError):
        module.verify_scheduler_rows(rows)


def test_i2_preflight_refuses_malformed_and_duplicate_schedulers() -> None:
    module = _module()
    for malformed in (None, {}, [], [object()], [{"name": "other"}]):
        with pytest.raises(module.I2PreflightError):
            module.verify_scheduler_rows(malformed)
    rows = _rows(module)
    rows[1]["name"] = rows[0]["name"]
    with pytest.raises(module.I2PreflightError):
        module.verify_scheduler_rows(rows)


def test_i2_preflight_rejects_unhashable_or_foreign_task_identity() -> None:
    module = _module()
    for bad in ([], {}, 5, None):
        rows = _rows(module)
        rows[1]["name"] = bad
        with pytest.raises(module.I2PreflightError):
            module.verify_scheduler_rows(rows)


def test_i2_pinned_native_wheel_integrity_before_first_mutation(tmp_path, monkeypatch) -> None:
    import hashlib

    from serverfs_mcp import windows_cloudflare_security

    module = _module()
    wheel = tmp_path / module.NATIVE_WHEEL_NAME
    contents = b"fixture-pinned-native-wheel"
    wheel.write_bytes(contents)
    monkeypatch.setattr(windows_cloudflare_security, "_no_reparse_components", lambda _: None)
    expected = hashlib.sha256(contents).hexdigest()
    monkeypatch.setattr(module, "NATIVE_WHEEL_SHA256", expected)
    assert module.verify_native_wheel(wheel) is None
    monkeypatch.setattr(module, "NATIVE_WHEEL_SHA256", "0" * 64)
    with pytest.raises(module.I2PreflightError, match="integrity"):
        module.verify_native_wheel(wheel)
    monkeypatch.setattr(module, "NATIVE_WHEEL_SHA256", expected)
    with pytest.raises(module.I2PreflightError):
        module.verify_native_wheel(wheel.with_name("foreign.whl"))
    wheel.unlink()
    with pytest.raises(module.I2PreflightError):
        module.verify_native_wheel(wheel)


def test_i2_offline_build_tool_checked_without_package_resolution(monkeypatch) -> None:
    import subprocess

    module = _module()
    commands = []

    monkeypatch.setattr(module.shutil, "which", lambda name: r"C:\tools\uv.exe")

    def fake_run(argv, **kwargs):
        commands.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"uv 0.9.0\n", b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.verify_offline_builder() is None
    assert len(commands) == 1
    assert commands[0][0] == [r"C:\tools\uv.exe", "--version"]
    assert commands[0][1]["stdin"] is subprocess.DEVNULL
    assert commands[0][1]["capture_output"] is True

    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    with pytest.raises(module.I2PreflightError, match="unavailable"):
        module.verify_offline_builder()


def test_i2_offline_build_tool_rejects_nonzero_or_unexpected_output(monkeypatch) -> None:
    import subprocess

    module = _module()
    monkeypatch.setattr(module.shutil, "which", lambda _: "uv.exe")
    for exit_code, stdout in ((1, b"uv 0.9.0"), (0, b"fake 1.2.3")):

        def fake_run(argv, exit_code=exit_code, stdout=stdout, **kwargs):
            return subprocess.CompletedProcess(argv, exit_code, stdout, b"")

        monkeypatch.setattr(module.subprocess, "run", fake_run)
        with pytest.raises(module.I2PreflightError):
            module.verify_offline_builder()


def test_i2_preflight_does_not_depend_on_prod_credentials() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for forbidden in (
        "http.env",
        "core-grants.json",
        "secret_hex",
        "private_key(",
        "storage.load(",
        "Scheduler().start(",
        "Scheduler().install(",
        "Scheduler().stop(",
    ):
        assert forbidden not in source
    assert "I2_NATIVE_PREFLIGHT=PASS" in source

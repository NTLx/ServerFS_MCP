"""Pure I2 protected Bridge staging contracts; no installer __main__ is executed."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[1] / "deployment" / "windows" / "v0250-i2-stage.py"


def _script():
    spec = importlib.util.spec_from_file_location("i2_source_only_installer", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i2_uses_unique_fixture_and_private_health_only() -> None:
    module = _script()
    assert module.INSTANCE == "i2-smoke"
    source = _SOURCE.read_text(encoding="utf-8")
    assert source.index("gate.main()") < source.index('storage.prepare("bridge", INSTANCE)')
    assert source.index("_assert_instance_isolation(manifest)") < source.index(
        "storage.stage(manifest)"
    )
    assert source.index("storage.stage(manifest)") < source.index("Scheduler().install(manifest)")
    assert '"read_only": True' in source
    assert '"agent_mode": "disabled"' in source
    assert '"methods": ["bridge.ping"]' in source
    assert "secrets.token_bytes(32)" in source
    assert "SERVERFS_HTTP_API_KEY" not in source
    assert "Scheduler().start(" not in source
    assert "Scheduler().stop(" not in source
    assert "Stop-Process" not in source
    assert "git push" not in source


def test_i2_stage_reuses_preflight_native_digest_before_any_file_write() -> None:
    source = _SOURCE.read_text(encoding="utf-8")
    assert (
        'native = REPO / "native" / "windows" / "target" / "wheels" / gate.NATIVE_WHEEL_NAME'
        in source
    )
    assert "gate.verify_native_wheel(native)" in source
    assert source.index("gate.verify_native_wheel(native)") < source.index(
        'storage.prepare("bridge", INSTANCE)'
    )
    assert "_NATIVE_SHA" not in source


def test_i2_native_runtime_imports_and_source_fingerprints_precede_credentials() -> None:
    source = _SOURCE.read_text(encoding="utf-8")
    assert "import serverfs_mcp.windows_v25.runtime, serverfs_windows_native, mcp" in source
    assert "import serverfs_agent_bridge.config" in source
    assert "serverfs_agent_bridge.core_grants_private" in source
    assert "installed.read_bytes() == source.read_bytes()" in source
    assert "I2_INSTALLED_RUNTIME=VERIFIED" in source
    assert source.index('STAGE = "installed-runtime-verification"') < source.index(
        'STAGE = "private-bridge-config"'
    )
    assert source.index('STAGE = "private-bridge-config"') < source.index(
        'STAGE = "private-grant-issuance"'
    )
    assert source.index("I2_INSTALLED_RUNTIME=VERIFIED") < source.index("storage.stage(manifest)")


def test_i2_installed_runtime_import_and_byte_gate_is_executable(tmp_path, monkeypatch) -> None:
    stage = _script()
    checkout = tmp_path / "checkout"
    monkeypatch.setattr(stage, "REPO", checkout)
    files = (
        (
            tmp_path
            / "envs"
            / "bridge"
            / "Lib"
            / "site-packages"
            / "serverfs_mcp"
            / "windows_v25"
            / "runtime.py",
            checkout / "src" / "serverfs_mcp" / "windows_v25" / "runtime.py",
        ),
        (
            tmp_path
            / "envs"
            / "agent-bridge"
            / "Lib"
            / "site-packages"
            / "serverfs_agent_bridge"
            / "core_grants_private.py",
            checkout / "agent_bridge" / "src" / "serverfs_agent_bridge" / "core_grants_private.py",
        ),
    )
    for installed, source in files:
        installed.parent.mkdir(parents=True, exist_ok=True)
        source.parent.mkdir(parents=True, exist_ok=True)
        installed.write_bytes(b"same-source-bytes")
        source.write_bytes(b"same-source-bytes")
    calls = []
    monkeypatch.setattr(stage, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    owner = tmp_path / "envs" / "bridge" / "Scripts" / "python.exe"
    worker = tmp_path / "envs" / "agent-bridge" / "Scripts" / "python.exe"
    assert stage.verify_installed_runtime(tmp_path, owner, worker) is None
    assert len(calls) == 2
    assert calls[0][0][:4] == [str(owner), "-I", "-B", "-c"]
    assert calls[1][0][:4] == [str(worker), "-I", "-B", "-c"]
    assert all(item[1]["timeout"] == 35 for item in calls)
    assert "serverfs_windows_native" in calls[0][0][4]
    assert "serverfs_agent_bridge.core_grants_private" in calls[1][0][4]
    files[1][0].write_bytes(b"tampered")
    with pytest.raises(stage.I2StageError, match="differs"):
        stage.verify_installed_runtime(tmp_path, owner, worker)
    files[1][0].unlink()
    with pytest.raises(stage.I2StageError, match="differs"):
        stage.verify_installed_runtime(tmp_path, owner, worker)


def test_i2_installer_provision_worker_calculates_policy_digest() -> None:
    module = _script()
    assert "core_workdir_policy_digest" in module._PROVISION
    assert "provision_private_core_grants" in module._PROVISION
    assert 'config.policies.get("Fixture")' in module._PROVISION
    assert "print(digest)" not in module._PROVISION
    assert "print(document)" not in module._PROVISION


def test_i2_uses_separate_venvs_and_offline_dry_run(tmp_path, monkeypatch) -> None:
    module = _script()
    venv = tmp_path / "venv"
    venv.mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ["uv", "venv"]:
            bin_dir = venv / "Scripts"
            bin_dir.mkdir()
            (bin_dir / "python.exe").touch()
            (bin_dir / "pythonw.exe").touch()
        return b""

    monkeypatch.setattr(module, "run", fake_run)
    wheel = tmp_path / "owner.whl"
    python, pythonw = module._offline_install(venv, [wheel])
    assert python.name == "python.exe"
    assert pythonw.name == "pythonw.exe"
    assert calls[0][0][:4] == ["uv", "venv", "--offline", "--python"]
    assert "--offline" in calls[1][0]
    assert calls[1][0][-1] == "--dry-run"
    assert calls[2][0] == calls[1][0][:-1]
    assert all(item[1].get("env", {}).get("UV_LINK_MODE") == "copy" for item in calls[1:])


def test_i2_subprocess_errors_hide_private_output(monkeypatch) -> None:
    module = _script()

    def denied(argv, **kwargs):
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(argv, 5, b"SECRET", b"SECRET")

    monkeypatch.setattr(module.subprocess, "run", denied)
    with pytest.raises(module.I2StageError) as caught:
        module.run(["uv", "build", "--offline"])
    assert "SECRET" not in str(caught.value)


def test_i2_private_provision_uses_stdin_not_cli_secret(monkeypatch) -> None:
    module = _script()
    seen = {}

    def accepted(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, b"I2_GRANT_INSTALLED", b"")

    monkeypatch.setattr(module.subprocess, "run", accepted)
    key = "f" * 64
    result = module.run(["python", "-I", "-B", "-c", "worker"], input_data=key.encode())
    assert result == b"I2_GRANT_INSTALLED"
    assert key not in repr(seen["argv"])
    assert seen["kwargs"]["input"] == key.encode()
    assert seen["kwargs"]["stdin"] is None


def test_i2_json_is_deterministic_and_no_implicit_disk_write() -> None:
    module = _script()
    payload = {"z": 1, "a": ["b"]}
    assert module._json(payload) == b'{"a":["b"],"z":1}\n'
    assert json.loads(module._json(payload)) == payload


def test_i2_no_acceptance_without_private_owner_and_disabled_task() -> None:
    source = _SOURCE.read_text(encoding="utf-8")
    for required in (
        "load_native_bridge_plan(manifest)",
        "gate.verify_scheduler_rows(gate._scheduler_inventory())",
        'storage.check_private(owner / "state" / "core-grants.json"',
        "I2_BRIDGE_TASK_REGISTERED_DISABLED=PASS",
        "I2_PRODUCTION_TASK_BASELINE=UNCHANGED",
    ):
        assert required in source

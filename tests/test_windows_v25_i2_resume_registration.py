"""I2 one-use native Bridge recovery contract; tests never use Windows Scheduler."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i2-resume-registration.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i2_resume_stage", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recovery_refuses_unapproved_artifacts_before_mutation() -> None:
    script = _SCRIPT.read_text(encoding="utf-8")
    assert 'INSTANCE = "i2-smoke"' in script
    assert '== "BRIDGE_OWNER_ONLY"' in script
    assert '"DIFFERS"' in script
    assert "verify_package_wheel(old_wheels[0]" in script
    assert 'not any((owner / "locks").iterdir())' in script
    assert "gate.verify_scheduler_rows(before)" in script
    assert script.index('print("I2_RESUME_ORIGINAL_STATE=VERIFIED")') < script.index(
        "stage._new_dir(rebuilt, sid)"
    )
    assert script.index("verify_package_wheel(wheel, venv") < script.index(
        "storage.stage(manifest)"
    )
    assert script.index("storage.stage(manifest)") < script.index("Scheduler().install(manifest)")
    assert script.index("gate.verify_scheduler_rows(gate._scheduler_inventory())") < script.index(
        "storage.stage(manifest)"
    )
    assert "stage.main(" not in script
    assert "stage._offline_install(" not in script
    assert all(f'"{option}"' in script for option in ("--offline", "--reinstall", "--no-deps"))
    assert "I2_NATIVE_STAGE_RECOVERED=PASS" in script


def test_private_wheel_rebuild_checks_all_code_bytes(tmp_path: Path, monkeypatch) -> None:
    from serverfs_mcp import windows_cloudflare_security

    stage = _module()
    monkeypatch.setattr(windows_cloudflare_security, "_no_reparse_components", lambda path: None)
    src = tmp_path / "src"
    target = tmp_path / "site-packages"
    name = "serverfs_mcp/windows_v25/bridge_v2_owner.py"
    (src / "serverfs_mcp" / "windows_v25").mkdir(parents=True)
    (target / "serverfs_mcp" / "windows_v25").mkdir(parents=True)
    (src / name).write_bytes(b"new-code")
    (target / name).write_bytes(b"old-code")
    wheel = tmp_path / "serverfs_mcp-0.25.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(name, b"new-code")
    fake = SimpleNamespace(check_private=lambda *args, **kwargs: None)
    assert stage.verify_package_wheel(wheel, None, "fake", fake, source_root=src) is None
    with pytest.raises(stage.I2ResumeError, match="differ"):
        stage.verify_package_wheel(wheel, target, "fake", fake)
    (target / name).write_bytes(b"new-code")
    assert stage.verify_package_wheel(wheel, target, "fake", fake) is None
    (src / name).write_bytes(b"changed-after-build")
    with pytest.raises(stage.I2ResumeError, match="differ"):
        stage.verify_package_wheel(wheel, None, "fake", fake, source_root=src)


def test_wheel_rejects_empty_python_package(tmp_path: Path, monkeypatch) -> None:
    from serverfs_mcp import windows_cloudflare_security

    stage = _module()
    monkeypatch.setattr(windows_cloudflare_security, "_no_reparse_components", lambda path: None)
    wheel = tmp_path / "empty.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dist-info/METADATA", "...")
    fake = SimpleNamespace(check_private=lambda *args, **kwargs: None)
    with pytest.raises(stage.I2ResumeError, match="no Python"):
        stage.verify_package_wheel(wheel, None, "fake", fake)

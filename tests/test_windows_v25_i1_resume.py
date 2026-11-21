"""Pure source tests for the Windows I1 existing-venv recovery.

Do not execute the runner's main(), access any protected fixture path,
read secrets, install wheels or operate Scheduled Tasks.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i1-resume-offline-install.py"
)


def resume_module():
    spec = importlib.util.spec_from_file_location("_i1_resume_tests", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recovery_is_exactly_one_known_fixture() -> None:
    script = resume_module()
    assert script.INSTANCE == "i1-smoke"
    assert script.PORT == 18868
    assert script.HOST == "i1-smoke.local"
    assert script._WHEEL == "serverfs_mcp-0.24.2-py3-none-any.whl"
    assert script._NATIVE_SHA.startswith("127a669511bc72e9")


def test_recovery_uses_offline_resolution_before_single_pip_install(monkeypatch, tmp_path) -> None:
    script = resume_module()
    invoked = []

    def fake_run(argv, *, env=None, timeout=120):
        invoked.append((argv, env, timeout))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(script, "_run", fake_run)
    python = tmp_path / "python.exe"
    product = tmp_path / "product.whl"
    native = tmp_path / "native.whl"
    script._offline_install(python, product, native)
    assert len(invoked) == 2
    probe, install = [item[0] for item in invoked]
    assert probe[:3] == ["uv", "pip", "install"]
    assert probe[3] == "--offline"
    assert "--no-index" not in probe
    assert "--dry-run" in probe
    assert install == [item for item in probe if item != "--dry-run"]
    assert all(item[1]["UV_LINK_MODE"] == "copy" for item in invoked)
    assert str(python) in install and str(product) in install and str(native) in install


def test_offline_resolution_failure_never_attempts_pip_mutation(
    monkeypatch, tmp_path, capsys
) -> None:
    script = resume_module()
    invocations: list[list[str]] = []

    def fake_run(argv, *, env=None, timeout=120):
        invocations.append(argv)
        return subprocess.CompletedProcess(
            argv, 1, b"PRIVATE PATH", b"No solution found when resolving dependencies"
        )

    monkeypatch.setattr(script, "_run", fake_run)
    with pytest.raises(script.ResumeRefusal, match="resolution is unavailable"):
        script._offline_install(
            tmp_path / "python.exe", tmp_path / "product.whl", tmp_path / "native.whl"
        )
    assert len(invocations) == 1 and "--dry-run" in invocations[0]
    printed = capsys.readouterr().out
    assert "offline-dependency-resolution" in printed
    assert "PRIVATE PATH" not in printed


def test_install_failure_does_not_dump_private_stderr(monkeypatch, tmp_path, capsys) -> None:
    script = resume_module()
    counter = 0

    def fake_run(argv, *, env=None, timeout=120):
        nonlocal counter
        counter += 1
        if counter == 1:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(
            argv, 2, b"SECRET API KEY", b"Permission denied at C:\\users\\private\\secret"
        )

    monkeypatch.setattr(script, "_run", fake_run)
    with pytest.raises(script.ResumeRefusal, match="installation failed"):
        script._offline_install(
            tmp_path / "python.exe", tmp_path / "product.whl", tmp_path / "native.whl"
        )
    output = capsys.readouterr().out
    assert "private-directory-permission" in output
    assert "SECRET" not in output
    assert "C:\\users" not in output


def test_recovery_rejects_untrusted_preexisting_identity_before_install() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "storage.check_private" in source
    assert "_trusted_existing_venv(stage, storage, sid, root)" in source
    assert "sha256(native) == _NATIVE_SHA" in source
    assert "version_info[:3] == (3, 12, 10)" in source
    assert "site / name).exists()" in source
    assert "production_snapshot()" in source
    assert 'root / "services" / "core" / "service.json"' in source
    assert "ctypes.windll.shell32.IsUserAnAdmin()" in source
    assert "storage.prepare(" not in source
    assert '"uv", "venv"' not in source
    assert '"uv", "build"' not in source
    assert "Remove-Item" not in source
    assert "SERVERFS_HTTP_API_KEY=" in source


def test_redacted_uv_failure_classification() -> None:
    script = resume_module()
    for message, category in [
        (b"No solution found", "offline-dependency-resolution"),
        (b"Access is denied", "private-directory-permission"),
        (b"incompatible wheel", "python-or-wheel-compatibility"),
        (b"unrecognized UNKNOWN", "unclassified-offline-install-error"),
    ]:
        assert script._uv_failure(SimpleNamespace(stderr=message)) == category

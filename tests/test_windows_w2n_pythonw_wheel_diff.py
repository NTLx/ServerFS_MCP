"""Synthetic W2j->W2n four-module-only wheel provenance checks."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest


def _gate():
    file = (
        Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2n-pythonw-wheel-diff.py"
    )
    spec = importlib.util.spec_from_file_location("w2n_wheel_diff", file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path, *, tamper: str = "") -> tuple[Path, Path, Path]:
    gate = _gate()
    source_root = tmp_path / "checkout"
    shared = {f"serverfs_mcp/synthetic_{i}.py": f"same-{i}".encode("ascii") for i in range(25)}
    shared["serverfs_mcp-0.24.2.dist-info/METADATA"] = b"Version: 0.24.2\n"
    shared["serverfs_mcp-0.24.2.dist-info/RECORD"] = b"old"
    original = dict(shared)
    updated = dict(shared)
    updated["serverfs_mcp-0.24.2.dist-info/RECORD"] = b"new"
    for i, member in enumerate(sorted(gate._MODULES)):
        source = source_root / "src" / member
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"new-file-{i}".encode())
        original[member] = f"old-file-{i}".encode()
        updated[member] = source.read_bytes()
    if tamper == "metadata":
        updated["serverfs_mcp-0.24.2.dist-info/METADATA"] = b"Version: 0.25.0\n"
    elif tamper == "other":
        updated["serverfs_mcp/synthetic_5.py"] = b"tampered-other"
    elif tamper == "missing":
        updated.pop("serverfs_mcp/synthetic_5.py")
    elif tamper == "source":
        (source_root / "src/serverfs_mcp/windows_process.py").write_bytes(b"foreign-source")
    old = tmp_path / "old.whl"
    new = tmp_path / "new.whl"
    for file, members in ((old, original), (new, updated)):
        with zipfile.ZipFile(file, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
    return old, new, source_root


def test_accepts_only_four_modules_and_record(tmp_path: Path) -> None:
    _gate().verify(*_fixture(tmp_path))


@pytest.mark.parametrize("tamper", ["metadata", "other", "missing", "source"])
def test_rejects_unreviewed_wheel_or_source_difference(tmp_path: Path, tamper: str) -> None:
    with pytest.raises(ValueError):
        _gate().verify(*_fixture(tmp_path, tamper=tamper))


def test_w2n_installer_mutation_is_core_only_after_four_module_diff() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "deployment/windows/v0250-w2n-update-core-candidate.ps1"
    ).read_text(encoding="utf-8")
    assert "core-owner-env" in script
    assert "v0250-w2n-pythonw-wheel-diff.py" in script
    assert "v0250-w2n-core-venv-home-gate.py" in script
    assert script.index("$stage = 'pre-install-Core-base-pythonw-origin'") < script.index(
        "$changeAttempted = $true"
    )
    assert "'--source-preflight'" in script
    assert (
        "$oldProductHash = '3102218157180fa7a70204156bbe9b85ba3b5b7a1d82cc40d214484ae04e007a'"
        in script
    )
    assert script.index("$stage = 'exact-four-source-module-wheel-diff'") < script.index(
        "$changeAttempted = $true"
    )
    assert script.index("Assert-FixedTasks") < script.index("$changeAttempted = $true")
    assert "'--no-deps','--reinstall','--link-mode','copy',$candidate" in script
    for forbidden in (
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Disable-ScheduledTask",
        "Enable-ScheduledTask",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "taskkill",
        "connector-env",
    ):
        assert forbidden not in script

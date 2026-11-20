"""Disposable synthetic W2j wheel-difference proof; no installed environments."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest


def _checker():
    source = Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2j-core-wheel-diff.py"
    spec = importlib.util.spec_from_file_location("w2j_core_wheel_diff", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _synthetic(tmp_path: Path, *, alter_other: bool = False) -> tuple[Path, Path, Path]:
    old = tmp_path / "old.whl"
    new = tmp_path / "new.whl"
    src = tmp_path / "windows_core_control.py"
    prefix = b'    name = ("Local'
    suffix = b'serverfs-mcp-core-v1-")\n'
    original = prefix + b"\\" * 4 + suffix
    corrected = prefix + b"\\" * 2 + suffix
    src.write_bytes(corrected)
    common = {f"serverfs_mcp/sample_{i}.py": f"unchanged{i}".encode() for i in range(24)}
    common["serverfs_mcp-0.24.2.dist-info/METADATA"] = b"Version: 0.24.2\n"
    common["serverfs_mcp-0.24.2.dist-info/RECORD"] = b"old-hash"
    original_members = {
        **common,
        "serverfs_mcp/windows_core_control.py": original,
    }
    updated_members = {
        **common,
        "serverfs_mcp/windows_core_control.py": corrected,
        "serverfs_mcp-0.24.2.dist-info/RECORD": b"new-hash",
    }
    if alter_other:
        updated_members["serverfs_mcp/sample_2.py"] = b"unknown-modification"
    for path, members in ((old, original_members), (new, updated_members)):
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
    return old, new, src


def test_accepts_only_the_mutex_module_and_record_diff(tmp_path: Path) -> None:
    old, new, source = _synthetic(tmp_path)
    _checker().verify(old, new, source)


def test_rejects_any_other_package_source_diff(tmp_path: Path) -> None:
    old, new, source = _synthetic(tmp_path, alter_other=True)
    with pytest.raises(ValueError, match="outside"):
        _checker().verify(old, new, source)


def test_rejects_source_that_differs_from_new_wheel(tmp_path: Path) -> None:
    old, new, source = _synthetic(tmp_path)
    source.write_bytes(b"not the corrected source")
    with pytest.raises(ValueError, match="differs"):
        _checker().verify(old, new, source)

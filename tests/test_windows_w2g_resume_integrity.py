"""Read-only W2g recovery verifier tests using synthetic disposable wheels."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest


def _helper():
    source = (
        Path(__file__).resolve().parents[1]
        / "deployment"
        / "windows"
        / "v0250-w2g-verify-installed.py"
    )
    spec = importlib.util.spec_from_file_location("w2g_resume_integrity", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheel_fixture(tmp_path: Path, *, package: str, native: bool) -> tuple[Path, Path]:
    site = tmp_path / "venv" / "Lib" / "site-packages"
    archive = tmp_path / f"{package}-0.24.2-py3-none-any.whl"
    entries = (
        {
            f"{package}/__init__.py": b"# only a synthetic test package\n",
            f"{package}/native.pyd": b"synthetic native bytes",
        }
        if native
        else {f"{package}/module_{i}.py": f"synthetic {i}".encode() for i in range(20)}
    )
    entries[f"{package}-0.24.2.dist-info/METADATA"] = f"Name: {package}\nVersion: 0.24.2\n".encode()
    with zipfile.ZipFile(archive, "w") as wheel:
        for name, data in entries.items():
            wheel.writestr(name, data)
            target = site.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return site, archive


def test_resumes_only_exact_product_wheel_bytes(tmp_path: Path) -> None:
    site, wheel = _wheel_fixture(tmp_path, package="serverfs_mcp", native=False)
    assert _helper().verify_one_wheel(site, wheel, "serverfs_mcp") == 21

    # A single unexpected byte must block an ACL repair/resume.
    (site / "serverfs_mcp" / "module_3.py").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="differs"):
        _helper().verify_one_wheel(site, wheel, "serverfs_mcp")


def test_blocks_unknown_executable_in_product_environment(tmp_path: Path) -> None:
    site, wheel = _wheel_fixture(tmp_path, package="serverfs_mcp", native=False)
    (site / "serverfs_mcp" / "unexpected.py").write_bytes(b"surprise")
    with pytest.raises(ValueError, match="unexpected executable"):
        _helper().verify_one_wheel(site, wheel, "serverfs_mcp")


def test_requires_matching_native_extension(tmp_path: Path) -> None:
    site, wheel = _wheel_fixture(tmp_path, package="serverfs_windows_native", native=True)
    assert _helper().verify_one_wheel(site, wheel, "serverfs_windows_native") == 3
    (site / "serverfs_windows_native" / "native.pyd").unlink()
    with pytest.raises(ValueError, match="missing"):
        _helper().verify_one_wheel(site, wheel, "serverfs_windows_native")


def test_blocks_reparse_or_shared_interpreter(tmp_path: Path) -> None:
    interpreter = tmp_path / "python.exe"
    interpreter.write_bytes(b"test")
    _helper()._unshared(interpreter)
    foreign_link = tmp_path / "python-link.exe"
    try:
        foreign_link.hardlink_to(interpreter)
    except OSError:
        pytest.skip("hardlinks are unavailable in this disposable test volume")
    with pytest.raises(ValueError, match="multiple hardlinks"):
        _helper()._unshared(interpreter)

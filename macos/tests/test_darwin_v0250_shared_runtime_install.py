"""Create-only offline macOS Shared Core runtime provisioning.

Synthetic tests do not execute uv, mutate real venvs or touch user HOME.
The opt-in test installs a full locked wheel into a private disposable /tmp
venv and validates that the signed-launcher Python pin is a regular file.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import darwin_shared_runtime as runtime


@pytest.fixture
def inputs(tmp_path: Path) -> dict:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='0.1'\n")
    (source / "uv.lock").write_text("synthetic-lock")
    parent = tmp_path / "install"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    base = tmp_path / "python-home"
    (base / "bin").mkdir(parents=True)
    (base / "lib").mkdir()
    python = base / "bin/python3.12"
    python.write_bytes(b"synthetic Python binary")
    python.chmod(0o700)
    dylib = base / "lib" / f"libpython{sys.version_info.major}.{sys.version_info.minor}.dylib"
    dylib.write_bytes(b"synthetic libpython")
    dylib.chmod(0o600)
    uv = tmp_path / "uv"
    uv.write_bytes(b"synthetic uv")
    uv.chmod(0o700)
    return {
        "target": parent / "shared-025",
        "source": source,
        "python": python,
        "uv": uv,
    }


def test_runtime_target_refuses_existing_or_non_private_without_deletion(inputs) -> None:
    c = inputs
    target = c["target"]
    target.mkdir()
    sentinel = target / "data.json"
    sentinel.write_text("preserve existing")
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="already exists"):
        runtime._private_target(target, c["source"])
    assert sentinel.read_text() == "preserve existing"
    sentinel.unlink()
    target.rmdir()
    target.parent.chmod(0o755)
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="private"):
        runtime._private_target(target, c["source"])


def test_runtime_target_cannot_install_inside_source(inputs) -> None:
    c = inputs
    project_parent = c["source"] / "runtime"
    project_parent.mkdir(mode=0o700)
    project_parent.chmod(0o700)
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="overlap"):
        runtime._private_target(project_parent / "shared", c["source"])


def test_runtime_input_requires_matching_dylib_and_locked_project(inputs) -> None:
    c = inputs
    base, dylib = runtime._checked_input(c["python"], c["source"], c["uv"])
    assert base == c["python"]
    assert dylib.is_file()
    dylib.unlink()
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="libpython"):
        runtime._checked_input(c["python"], c["source"], c["uv"])


def test_uv_subprocess_cannot_inherit_credentials_or_pythonpath(
    monkeypatch: pytest.MonkeyPatch, inputs
) -> None:
    c = inputs
    monkeypatch.setenv("SERVERFS_HTTP_API_KEY", "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", "/danger/ambient")
    monkeypatch.setenv("UV_INDEX_URL", "https://private-index.invalid")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    runtime._execute_uv(c["uv"], ["export", "--offline"], source=c["source"], step="test")
    assert seen["argv"] == [str(c["uv"]), "export", "--offline"]
    assert seen["cwd"] == c["source"]
    for key in ("SERVERFS_HTTP_API_KEY", "PYTHONPATH", "UV_INDEX_URL"):
        assert key not in seen["env"]
    assert seen["env"]["UV_OFFLINE"] == "1"
    assert seen["stdout"] == runtime.subprocess.DEVNULL
    assert seen["stderr"] == runtime.subprocess.DEVNULL


@pytest.mark.parametrize("mode", ["build-failure", "probe-failure", "post-publish-failure"])
def test_runtime_install_rolls_back_only_its_new_candidate(
    monkeypatch: pytest.MonkeyPatch, inputs, mode: str
) -> None:
    c = inputs
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    sibling = c["target"].parent / "protected-existing"
    sibling.mkdir()
    protected = sibling / "do-not-edit"
    protected.write_text("untouched")

    def fake_uv(_uv, args, *, source, step, timeout=150):
        if mode == "build-failure" and step == "wheel build":
            raise runtime.DarwinSharedRuntimeError("build failed")
        if step == "lock export":
            assert "--offline" in args and "--locked" in args
            assert "--frozen" not in args
            output = Path(args[args.index("--output-file") + 1])
            output.write_text("synthetic requirements", encoding="utf-8")
        elif step == "wheel build":
            dist = Path(args[args.index("--out-dir") + 1])
            dist.mkdir()
            (dist / "serverfs_mcp-0.1-py3-none-any.whl").write_bytes(b"fake-wheel")
        elif step == "venv creation":
            path = Path(args[-1])
            (path / "bin").mkdir(parents=True)
            (path / "lib").mkdir()
            packages = (
                path
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            packages.mkdir(parents=True)
            (packages / "dependency.py").write_bytes(b"dependency = True\n")

    monkeypatch.setattr(runtime, "_execute_uv", fake_uv)
    monkeypatch.setattr(runtime, "_publish_new_directory", os.rename)
    monkeypatch.setattr(runtime, "_validate_shared_core_python", lambda path: path)
    probes = []

    def fake_probe(path: Path) -> None:
        probes.append(path)
        if mode == "probe-failure" or (mode == "post-publish-failure" and len(probes) == 2):
            raise runtime.DarwinSharedRuntimeError("probe refused")

    monkeypatch.setattr(runtime, "_probe", fake_probe)
    with pytest.raises(runtime.DarwinSharedRuntimeError):
        runtime.create_shared_core_runtime(c["target"], c["source"], c["python"], c["uv"])
    assert not c["target"].exists()
    assert protected.read_text() == "untouched"
    assert not any(
        item.name.startswith(".shared-core-build-") for item in c["target"].parent.iterdir()
    )


def test_runtime_install_success_is_create_only(inputs, monkeypatch: pytest.MonkeyPatch) -> None:
    c = inputs
    monkeypatch.setattr(runtime.sys, "platform", "darwin")

    def fake_uv(_uv, args, *, source, step, timeout=150):
        if step == "lock export":
            assert "--offline" in args and "--locked" in args
            assert "--frozen" not in args
            Path(args[args.index("--output-file") + 1]).write_text("locked")
        elif step == "wheel build":
            dist = Path(args[args.index("--out-dir") + 1])
            dist.mkdir()
            (dist / "serverfs_mcp-0.1-py3-none-any.whl").write_bytes(b"wheel")
        elif step == "venv creation":
            path = Path(args[-1])
            (path / "bin").mkdir(parents=True)
            (path / "lib").mkdir()
            packages = (
                path
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            packages.mkdir(parents=True)
            (packages / "dependency.py").write_bytes(b"dependency = True\n")

    monkeypatch.setattr(runtime, "_execute_uv", fake_uv)
    monkeypatch.setattr(runtime, "_publish_new_directory", os.rename)
    monkeypatch.setattr(runtime, "_validate_shared_core_python", lambda path: path)
    monkeypatch.setattr(runtime, "_probe", lambda candidate: None)
    result = runtime.create_shared_core_runtime(c["target"], c["source"], c["python"], c["uv"])
    assert result["state"] == "installed"
    assert result["python"] == str(c["target"] / "bin/serverfs-core-python")
    executable = c["target"] / "bin/serverfs-core-python"
    assert stat.S_ISREG(executable.lstat().st_mode) and not executable.is_symlink()
    assert (
        c["target"] / "lib" / f"libpython{sys.version_info.major}.{sys.version_info.minor}.dylib"
    ).exists()
    record = c["target"] / "runtime-provenance.json"
    provenance = json.loads(record.read_text())
    assert provenance["offline"] is True
    assert provenance["schema_version"] == 3
    assert provenance["site_packages_file_count"] == 2  # dependency + sitecustomize
    assert (
        c["target"]
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "sitecustomize.py"
    ).is_file()
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert (
        runtime.verify_shared_core_runtime(c["target"], project_root=c["source"])["state"]
        == "verified"
    )
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="already exists"):
        runtime.create_shared_core_runtime(c["target"], c["source"], c["python"], c["uv"])


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin renamex_np exclusive API")
def test_atomic_publication_never_replaces_existing_empty_directory(tmp_path: Path) -> None:
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "sentinel").write_text("keep")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="already exists"):
        runtime._publish_new_directory(source, occupied)
    assert (source / "sentinel").read_text() == "keep"
    assert not any(occupied.iterdir())


@pytest.mark.skipif(
    sys.platform != "darwin"
    or os.environ.get("SERVERFS_ENABLE_SHARED_RUNTIME_INSTALL_TEST") != "1",
    reason="requires explicit offline macOS uv + pinned wheel runtime install",
)
def test_real_locked_runtime_without_source_or_test_dependencies() -> None:
    repo = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    assert uv is not None
    with tempfile.TemporaryDirectory(prefix="sf-runtime-real-", dir="/tmp") as temp:
        private = Path(temp).resolve(strict=True)
        private.chmod(0o700)
        target = private / "shared-core"
        result = runtime.create_shared_core_runtime(
            target, repo, Path(sys.executable), Path(uv).resolve(strict=True)
        )
        pinned = Path(result["python"])
        assert pinned.is_file() and not pinned.is_symlink()
        assert pinned == (target / "bin/serverfs-core-python")
        runtime._probe(target)
        assert runtime.verify_shared_core_runtime(target, project_root=repo)["state"] == "verified"
        assert stat.S_IMODE((target / "runtime-provenance.json").stat().st_mode) == 0o600
        with pytest.raises(runtime.DarwinSharedRuntimeError, match="already exists"):
            runtime.create_shared_core_runtime(
                target, repo, Path(sys.executable), Path(uv).resolve(strict=True)
            )

"""Portable tests for offline named Windows runtime installer; no OS mutations."""

from __future__ import annotations

import hashlib
import importlib.util
import tomllib
import zipfile
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "deployment/windows/install-v25-runtime.py"
SPEC = importlib.util.spec_from_file_location("windows_v25_runtime_installer", TOOL)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.mark.parametrize("component", ["core", "cloudflare", "openai"])
def test_each_component_has_only_its_independent_owner(component):
    assert installer.plan(component) == ((component, ("core", "native")),)


def test_bridge_has_a_separate_worker_environment():
    assert installer.plan("bridge") == (
        ("bridge", ("core", "native")),
        ("agent-bridge", ("bridge",)),
    )


def test_installer_version_matches_current_project():
    root = TOOL.parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert installer.VERSION == project["project"]["version"]


def test_unknown_component_refused():
    with pytest.raises(installer.InstallRefusal):
        installer.plan("default")


def make_wheel(tmp_path, kind, *, version=installer.VERSION, extra=None, name=None):
    filename, dist, package = installer.WHEELS[kind]
    path = tmp_path / (name or filename)
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(
            dist.replace("-", "_") + f"-{installer.VERSION}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n",
        )
        wheel.writestr(package + "/__init__.py", "")
        if extra:
            # ZipInfo normalizes backslashes on Windows during construction.
            # Assign the raw filename after construction to exercise the wire name.
            info = zipfile.ZipInfo("synthetic")
            info.filename = extra
            wheel.writestr(info, "synthetic")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("kind", ["core", "native", "bridge"])
def test_matching_pinned_wheel_bytes(kind, tmp_path):
    path, sha = make_wheel(tmp_path, kind)
    assert installer.pinned_payload(path, kind, sha) == path.read_bytes()


@pytest.mark.parametrize("digest", ["bad", "0" * 64, "A" * 64])
def test_invalid_hash_refused(tmp_path, digest):
    path, _ = make_wheel(tmp_path, "core")
    with pytest.raises(installer.InstallRefusal):
        installer.pinned_payload(path, "core", digest)


def test_wrong_release_metadata_refused_even_with_matching_hash(tmp_path):
    path, sha = make_wheel(tmp_path, "core", version="0.24.2")
    with pytest.raises(installer.InstallRefusal, match="identity"):
        installer.pinned_payload(path, "core", sha)


def test_renamed_wheel_refused(tmp_path):
    path, sha = make_wheel(tmp_path, "core", name="other.whl")
    with pytest.raises(installer.InstallRefusal):
        installer.pinned_payload(path, "core", sha)


@pytest.mark.parametrize("member", ["../escape", "/escape", "C:/escape", "x\\escape"])
def test_archive_path_refused(tmp_path, member):
    path, sha = make_wheel(tmp_path, "core", extra=member)
    with pytest.raises(installer.InstallRefusal, match="member path"):
        installer.pinned_payload(path, "core", sha)


def test_fresh_environment_is_accepted_but_existing_bytes_are_not_replaced(tmp_path):
    installer.ensure_empty(tmp_path)
    existing = tmp_path / "existing"
    existing.write_bytes(b"keep")
    with pytest.raises(installer.InstallRefusal, match="fresh and empty"):
        installer.ensure_empty(tmp_path)
    assert existing.read_bytes() == b"keep"


def test_missing_environment_refused(tmp_path):
    with pytest.raises(installer.InstallRefusal):
        installer.ensure_empty(tmp_path / "absent")


def test_install_subprocess_failure_does_not_expose_raw_output(monkeypatch, tmp_path):
    from types import SimpleNamespace

    def refused(*args, **kwargs):
        assert kwargs["stdin"] == installer.subprocess.DEVNULL
        assert kwargs["capture_output"] is True
        return SimpleNamespace(returncode=1, stdout=b"private", stderr=b"private")

    monkeypatch.setattr(installer.subprocess, "run", refused)
    with pytest.raises(installer.InstallRefusal) as exc:
        installer.run(["synthetic"], {}, tmp_path, 1)
    assert "private" not in str(exc.value)


def test_offline_environment_retains_only_explicit_uv_cache():
    result = installer.offline_env(
        {
            "PATH": "synthetic",
            "UV_CACHE_DIR": "custom-cache",
            "UV_OFFLINE": "0",
            "UV_LINK_MODE": "hardlink",
            "UV_INDEX_URL": "not-forwarded",
            "ANTHROPIC_API_KEY": "not-forwarded",
            "PYTHONPATH": "not-forwarded",
        }
    )
    assert result == {
        "PATH": "synthetic",
        "UV_CACHE_DIR": "custom-cache",
        "UV_OFFLINE": "1",
        "UV_LINK_MODE": "copy",
        "UV_NO_PROGRESS": "1",
    }


def test_default_uv_cache_is_not_overridden():
    assert "UV_CACHE_DIR" not in installer.offline_env({})

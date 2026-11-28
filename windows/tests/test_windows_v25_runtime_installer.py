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


def make_wheel(tmp_path, kind, *, version=installer.VERSION, extra=None, name=None, tag=None):
    filename, dist, package = installer.WHEELS[kind]
    path = tmp_path / (name or filename)
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(
            dist.replace("-", "_") + f"-{installer.VERSION}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n",
        )
        if tag is None:
            tag = "cp312-abi3-win_amd64" if kind == "native" else "py3-none-win_amd64"
        wheel.writestr(
            dist.replace("-", "_") + f"-{installer.VERSION}.dist-info/WHEEL",
            f"Wheel-Version: 1.0\nTag: {tag}\n",
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


@pytest.mark.parametrize("kind", ["core", "bridge"])
@pytest.mark.parametrize(
    "tag", ["py3-none-any", "py3-none-macosx_27_0_arm64", "py3-none-linux_x86_64"]
)
def test_wrong_platform_tag_refused_even_with_matching_filename_and_hash(tmp_path, kind, tag):
    path, sha = make_wheel(tmp_path, kind, tag=tag)
    with pytest.raises(installer.InstallRefusal, match="platform tag"):
        installer.pinned_payload(path, kind, sha)


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


def make_runtime_lock(tmp_path, *, distribution="serverfs-mcp", version=installer.VERSION):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "{distribution}"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        f'[[package]]\nname = "{distribution}"\nversion = "{version}"\n'
        'source = { editable = "." }\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize("distribution", ["serverfs-mcp", "serverfs-agent-bridge"])
def test_runtime_constraints_export_uses_matching_frozen_lock(tmp_path, monkeypatch, distribution):
    make_runtime_lock(tmp_path, distribution=distribution)
    exported = b'# generated\nanyio==4.15.1\npydantic-core==2.46.5 ; sys_platform == "win32"\n'
    env = installer.offline_env({"PATH": "synthetic"})

    def export(argv, passed_env, cwd, timeout):
        assert argv == [
            "uv",
            "--no-config",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements-txt",
        ]
        assert passed_env == env
        assert cwd == tmp_path
        assert timeout == 120
        return exported

    monkeypatch.setattr(installer, "run", export)
    assert installer.runtime_constraints("uv", tmp_path, distribution, env) == exported


@pytest.mark.parametrize(
    "exported",
    [
        b"",
        b"# empty\n",
        b"anyio>=4\n",
        b"-e .\n",
        b"--index-url https://example.invalid/simple\n",
        b"anyio @ https://example.invalid/a.whl\n",
        b"\xff",
    ],
)
def test_runtime_constraints_refuse_non_pinned_or_invalid_export(tmp_path, monkeypatch, exported):
    make_runtime_lock(tmp_path)
    monkeypatch.setattr(installer, "run", lambda *args: exported)
    with pytest.raises(installer.InstallRefusal, match="runtime lock export"):
        installer.runtime_constraints("uv", tmp_path, "serverfs-mcp", {})


@pytest.mark.parametrize(
    "version,distribution",
    [("0.25.0", "serverfs-mcp"), (installer.VERSION, "serverfs-agent-bridge")],
)
def test_runtime_constraints_refuse_mismatched_project(
    tmp_path, monkeypatch, version, distribution
):
    make_runtime_lock(tmp_path, distribution=distribution, version=version)

    def unexpected(*args):
        pytest.fail("must refuse before invoking uv")

    monkeypatch.setattr(installer, "run", unexpected)
    with pytest.raises(installer.InstallRefusal, match="lock identity"):
        installer.runtime_constraints("uv", tmp_path, "serverfs-mcp", {})


def test_runtime_constraints_refuse_missing_lock(tmp_path):
    with pytest.raises(installer.InstallRefusal, match="matching runtime lock"):
        installer.runtime_constraints("uv", tmp_path, "serverfs-mcp", {})


def test_locked_install_materializes_dependencies_then_local_wheels(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        installer, "run", lambda argv, env, cwd, timeout: calls.append((argv, env, cwd, timeout))
    )
    py = tmp_path / "python.exe"
    requirements = tmp_path / "requirements.txt"
    wheels = [tmp_path / installer.WHEELS[k][0] for k in ("core", "native")]
    env = installer.offline_env({"PATH": "synthetic"})
    installer.install_locked_runtime("uv", py, requirements, wheels, env, tmp_path)
    base = ["uv", "--no-config", "pip", "install", "--offline", "--python", str(py)]
    assert calls == [
        (
            [*base, "--no-deps", "--only-binary", ":all:", "-r", str(requirements)],
            env,
            tmp_path,
            240,
        ),
        ([*base, "--no-deps", *map(str, wheels)], env, tmp_path, 240),
    ]


def test_failed_locked_dependency_install_never_installs_project_wheels(tmp_path, monkeypatch):
    calls = []

    def refuse(argv, *args):
        calls.append(argv)
        raise installer.InstallRefusal("offline runtime subprocess failed")

    monkeypatch.setattr(installer, "run", refuse)
    with pytest.raises(installer.InstallRefusal):
        installer.install_locked_runtime(
            "uv",
            tmp_path / "python.exe",
            tmp_path / "requirements.txt",
            [tmp_path / "core.whl"],
            {},
            tmp_path,
        )
    assert len(calls) == 1


def test_locked_inventory_accepts_only_exported_versions_and_products():
    requirements = b'anyio==4.15.1\nrpds-py==2026.6.3 ; sys_platform == "win32"\n'
    products = [
        ["serverfs-mcp", installer.VERSION],
        ["serverfs-windows-native", installer.VERSION],
    ]
    assert (
        installer.locked_inventory(
            requirements,
            ("core", "native"),
            products + [["AnyIO", "4.15.1"], ["rpds_py", "2026.6.3"]],
        )
        == 2
    )
    # Marker selection is performed by uv; inactive conditional pins need not be installed.
    assert (
        installer.locked_inventory(
            requirements,
            ("core", "native"),
            products + [["anyio", "4.15.1"]],
        )
        == 1
    )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [["anyio", "4.15.0"]],
        [["anyio", "4.15.1"], ["unlocked", "1.0"]],
        [["anyio", "4.15.1"], ["ANYIO", "4.15.1"]],
        [["anyio", "4.15.1"], ["rpds-py", "2026.6.2"]],
        [["anyio", "4.15.1"], ["serverfs-agent-bridge", installer.VERSION]],
    ],
)
def test_locked_inventory_refuses_missing_drift_duplicate_or_unlocked(rows):
    requirements = b'anyio==4.15.1\nrpds-py==2026.6.3 ; sys_platform == "win32"\n'
    products = [
        ["serverfs-mcp", installer.VERSION],
        ["serverfs-windows-native", installer.VERSION],
    ]
    with pytest.raises(installer.InstallRefusal):
        installer.locked_inventory(requirements, ("core", "native"), products + rows)


def test_locked_inventory_refuses_project_in_dependency_export():
    with pytest.raises(installer.InstallRefusal, match="overlaps"):
        installer.locked_inventory(b"serverfs-mcp==0.26.0\n", ("core", "native"), [])

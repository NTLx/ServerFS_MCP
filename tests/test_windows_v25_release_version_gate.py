"""Deterministic tests for the Windows v0.25.0 read-only version gate."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment" / "windows" / "v0250-release-version-gate.py"
)
spec = importlib.util.spec_from_file_location("windows_release_version_gate", SCRIPT)
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def fixture_root(tmp_path: Path, version: str = "0.25.0") -> Path:
    (tmp_path / "agent_bridge").mkdir()
    (tmp_path / "native" / "windows").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "serverfs-mcp"\nversion = "{version}"\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        f'[[package]]\nname = "serverfs-mcp"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "agent_bridge" / "pyproject.toml").write_text(
        f'[project]\nname = "serverfs-agent-bridge"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "agent_bridge" / "uv.lock").write_text(
        f'[[package]]\nname = "serverfs-agent-bridge"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "native" / "windows" / "Cargo.toml").write_text(
        f'[package]\nname = "serverfs-windows-native"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "native" / "windows" / "Cargo.lock").write_text(
        f'[[package]]\nname = "serverfs-windows-native"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "plugin.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (tmp_path / "server.json").write_text(
        json.dumps(
            {
                "version": version,
                "packages": [
                    {
                        "registryType": "oci",
                        "identifier": f"ghcr.io/ntlx/serverfs_mcp:{version}",
                        "version": version,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_exact_release_coherent_and_ready(tmp_path: Path) -> None:
    records = gate.collect_versions(fixture_root(tmp_path))
    assert len(records) == 10
    assert gate.audit(records, expected="0.25.0") == (True, True)


def test_current_stable_coherent_but_not_released(tmp_path: Path) -> None:
    records = gate.collect_versions(fixture_root(tmp_path, "0.24.2"))
    assert gate.audit(records, expected="0.25.0") == (True, False)
    assert gate.audit(records, expected="0.24.2") == (True, True)


@pytest.mark.parametrize(
    ("path", "from_value", "to_value"),
    [
        ("uv.lock", "0.25.0", "0.24.2"),
        ("agent_bridge/pyproject.toml", "0.25.0", "0.24.2"),
        ("agent_bridge/uv.lock", "0.25.0", "0.24.2"),
        ("native/windows/Cargo.toml", "0.25.0", "0.24.2"),
        ("native/windows/Cargo.lock", "0.25.0", "0.24.2"),
        ("plugin.json", "0.25.0", "0.24.2"),
    ],
)
def test_any_mismatched_source_blocks(
    tmp_path: Path, path: str, from_value: str, to_value: str
) -> None:
    root = fixture_root(tmp_path)
    target = root / path
    target.write_text(
        target.read_text(encoding="utf-8").replace(from_value, to_value),
        encoding="utf-8",
    )
    assert gate.audit(gate.collect_versions(root), expected="0.25.0") == (False, False)


@pytest.mark.parametrize("field", ("root", "package", "tag"))
def test_registry_version_drift_blocks(tmp_path: Path, field: str) -> None:
    root = fixture_root(tmp_path)
    target = root / "server.json"
    registry = json.loads(target.read_text(encoding="utf-8"))
    if field == "root":
        registry["version"] = "0.24.2"
    elif field == "package":
        registry["packages"][0]["version"] = "0.24.2"
    else:
        registry["packages"][0]["identifier"] = "ghcr.io/ntlx/serverfs_mcp:0.24.2"
    target.write_text(json.dumps(registry), encoding="utf-8")
    records = gate.collect_versions(root)
    assert len(set(records.values())) == 2
    assert gate.audit(records, expected="0.25.0") == (False, False)


@pytest.mark.parametrize(
    "bad_form",
    (
        "missing",
        "not_object",
        "missing_packages",
        "multiple_packages",
        "non_oci",
        "bad_tag",
        "digest",
    ),
)
def test_invalid_registry_package_fails_closed(tmp_path: Path, bad_form: str) -> None:
    root = fixture_root(tmp_path)
    target = root / "server.json"
    registry = json.loads(target.read_text(encoding="utf-8"))
    if bad_form == "missing":
        target.unlink()
    elif bad_form == "not_object":
        registry = []
    elif bad_form == "missing_packages":
        del registry["packages"]
    elif bad_form == "multiple_packages":
        registry["packages"].append(dict(registry["packages"][0]))
    elif bad_form == "non_oci":
        registry["packages"][0]["registryType"] = "npm"
    elif bad_form == "bad_tag":
        registry["packages"][0]["identifier"] = "ghcr.io/ntlx/serverfs_mcp:latest"
    elif bad_form == "digest":
        registry["packages"][0]["identifier"] = "ghcr.io/ntlx/serverfs_mcp@sha256:abcd"
    if bad_form != "missing":
        target.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises((OSError, gate.VersionGateError)):
        gate.collect_versions(root)


def test_missing_lock_package_fails_closed(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    (root / "uv.lock").write_text(
        '[[package]]\nname = "unrelated"\nversion = "0.25.0"\n', encoding="utf-8"
    )
    with pytest.raises(gate.VersionGateError):
        gate.collect_versions(root)


def test_duplicate_lock_package_fails_closed(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    target = root / "uv.lock"
    target.write_text(target.read_text(encoding="utf-8") * 2, encoding="utf-8")
    with pytest.raises(gate.VersionGateError):
        gate.collect_versions(root)


def test_invalid_or_missing_version_fails_closed(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    (root / "plugin.json").write_text('{"version": null}', encoding="utf-8")
    with pytest.raises(gate.VersionGateError):
        gate.collect_versions(root)


def test_incomplete_source_map_fails_closed() -> None:
    with pytest.raises(gate.VersionGateError):
        gate.audit({"core_project": "0.25.0"}, expected="0.25.0")

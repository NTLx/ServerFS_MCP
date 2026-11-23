"""Focused stable-release metadata consistency tests."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
CURRENT_VERSION = PROJECT["project"]["version"]
CURRENT_TAG = f"v{CURRENT_VERSION}"
MISMATCH_VERSION = "0.0.0" if CURRENT_VERSION != "0.0.0" else "0.0.1"
_CHECKER = ROOT / "deployment/native/release_metadata.py"
_SPEC = importlib.util.spec_from_file_location("release_metadata", _CHECKER)
assert _SPEC is not None and _SPEC.loader is not None
release_metadata = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = release_metadata
_SPEC.loader.exec_module(release_metadata)
MetadataError = release_metadata.MetadataError
validate = release_metadata.validate
FILES = (
    "pyproject.toml",
    "uv.lock",
    "agent_bridge/pyproject.toml",
    "agent_bridge/uv.lock",
    "native/windows/Cargo.toml",
    "native/windows/Cargo.lock",
    "plugin.json",
    "server.json",
)


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    for relative in FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    return tmp_path


def update_json(path: Path, mutate) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_current_release_baseline_is_consistent(fixture_root: Path) -> None:
    result = validate(fixture_root, CURRENT_TAG)
    assert result["root"] == result["registry"] == CURRENT_VERSION


def test_nested_registry_package_version_must_match(fixture_root: Path) -> None:
    update_json(
        fixture_root / "server.json", lambda d: d["packages"][0].update(version=MISMATCH_VERSION)
    )
    with pytest.raises(MetadataError, match=r"packages\[0\]\.version"):
        validate(fixture_root, CURRENT_TAG)


def test_missing_nested_registry_package_version_fails(fixture_root: Path) -> None:
    update_json(fixture_root / "server.json", lambda d: d["packages"][0].pop("version"))
    with pytest.raises(MetadataError, match=r"packages\[0\]\.version"):
        validate(fixture_root, CURRENT_TAG)


def test_oci_tag_must_match_release_tag(fixture_root: Path) -> None:
    update_json(
        fixture_root / "server.json",
        lambda d: d["packages"][0].update(
            identifier=f"ghcr.io/ntlx/serverfs_mcp:{MISMATCH_VERSION}"
        ),
    )
    with pytest.raises(MetadataError, match=r"packages\[0\]\.identifier"):
        validate(fixture_root, CURRENT_TAG)


@pytest.mark.parametrize(
    ("relative", "name"),
    [
        ("uv.lock", "serverfs-mcp"),
        ("agent_bridge/uv.lock", "serverfs-agent-bridge"),
        ("native/windows/Cargo.lock", "serverfs-windows-native"),
    ],
)
def test_missing_local_lock_record_fails(fixture_root: Path, relative: str, name: str) -> None:
    path = fixture_root / relative
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    records = data["package"]
    start = path.read_text(encoding="utf-8").find('name = "' + name + '"')
    assert start >= 0
    # Fixture-specific removal keeps this test independent of third-party graph formatting.
    chunks = path.read_text(encoding="utf-8").split("[[package]]")
    chunks = [chunk for chunk in chunks if f'name = "{name}"' not in chunk]
    path.write_text("[[package]]".join(chunks), encoding="utf-8")
    assert len(records) > 0
    with pytest.raises(MetadataError, match="exactly one local package record"):
        validate(fixture_root, CURRENT_TAG)


@pytest.mark.parametrize(
    ("relative", "name"),
    [
        ("uv.lock", "serverfs-mcp"),
        ("agent_bridge/uv.lock", "serverfs-agent-bridge"),
        ("native/windows/Cargo.lock", "serverfs-windows-native"),
    ],
)
def test_stale_local_lock_record_fails(fixture_root: Path, relative: str, name: str) -> None:
    path = fixture_root / relative
    content = path.read_text(encoding="utf-8")
    package = f'name = "{name}"\nversion = "{CURRENT_VERSION}"'
    assert content.count(package) == 1
    path.write_text(content.replace(package, f'name = "{name}"\nversion = "{MISMATCH_VERSION}"'))
    with pytest.raises(MetadataError, match=f"expected {CURRENT_VERSION!r}"):
        validate(fixture_root, CURRENT_TAG)


def test_malformed_tag_fails_closed(fixture_root: Path) -> None:
    for tag in ("0.24.2", "v0.24", "v01.2.3", "v0.24.2-rc1", "v0.24.2\n"):
        with pytest.raises(MetadataError, match="malformed release tag"):
            validate(fixture_root, tag)

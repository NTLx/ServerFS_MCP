from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "macos"
    / "v0250_wheel_offline_preflight.py"
)
SPEC = importlib.util.spec_from_file_location("wheel_offline_preflight", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _wheel(path: Path, name: str, version: str, requirements: list[str]) -> None:
    metadata = [f"Name: {name}", f"Version: {version}"]
    metadata.extend(f"Requires-Dist: {requirement}" for requirement in requirements)
    metadata_path = f"{name.replace('-', '_')}-{version}.dist-info/METADATA"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(metadata_path, "\n".join(metadata) + "\n")


def _installed_distribution(path: Path, name: str, version: str) -> None:
    dist_info = path / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )


def test_distinguishes_installed_distribution_from_missing_cache(tmp_path: Path) -> None:
    wheel = tmp_path / "serverfs_mcp-0.24.2-py3-none-any.whl"
    _wheel(wheel, "serverfs-mcp", "0.24.2", ["mcp==2.3.0", "uvicorn>=0.30"])
    environment = tmp_path / "site-packages"
    _installed_distribution(environment, "mcp", "2.3.0")
    _installed_distribution(environment, "uvicorn", "0.53.0")

    result = MODULE.inspect_wheel(wheel, environment, tmp_path / "empty-cache")

    assert result == [("mcp==2.3.0", True, False), ("uvicorn>=0.30", True, False)]
    assert MODULE._label(*result[0]) == "installed-only"


def test_reports_wrong_installed_version_and_matching_cached_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "bridge.whl"
    _wheel(wheel, "serverfs-agent-bridge", "0.24.2", ["websockets==17.1"])
    environment = tmp_path / "site-packages"
    _installed_distribution(environment, "websockets", "16.0")
    cache = tmp_path / "cache"
    cache.mkdir()
    _wheel(cache / "websockets-17.1-py3-none-any.whl", "websockets", "17.1", [])

    assert MODULE.inspect_wheel(wheel, environment, cache) == [("websockets==17.1", False, True)]

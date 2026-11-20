"""Read-only release version coherence gate for the Windows v0.25.0 candidate.

No lock regeneration, package installation or working-tree mutation.
"""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

RELEASE_VERSION = "0.25.0"
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?\Z")
SOURCES = (
    "core_project",
    "core_lock",
    "bridge_project",
    "bridge_lock",
    "windows_native_project",
    "windows_native_lock",
    "plugin",
    "registry_root",
    "registry_oci_package",
    "registry_oci_tag",
)


class VersionGateError(ValueError):
    """An expected source is missing or has an invalid version contract."""


def _read_toml(path: Path) -> dict:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if not isinstance(data, dict):
        raise VersionGateError("invalid TOML document")
    return data


def _package_version(path: Path, package_name: str) -> str:
    packages = _read_toml(path).get("package")
    if not isinstance(packages, list):
        raise VersionGateError("lockfile packages missing")
    matches = [
        item for item in packages if isinstance(item, dict) and item.get("name") == package_name
    ]
    if len(matches) != 1:
        raise VersionGateError("lockfile local package missing or duplicated")
    return _valid_version(matches[0].get("version"))


def _valid_version(value: object) -> str:
    if not isinstance(value, str) or VERSION_PATTERN.fullmatch(value) is None:
        raise VersionGateError("invalid package version")
    return value


def _registry_versions(path: Path) -> tuple[str, str, str]:
    """Verify the MCP Registry root, OCI package and referenced image tag."""
    registry = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(registry, dict):
        raise VersionGateError("invalid MCP Registry metadata")
    packages = registry.get("packages")
    if not isinstance(packages, list) or len(packages) != 1 or not isinstance(packages[0], dict):
        raise VersionGateError("MCP Registry requires exactly one OCI package")
    package = packages[0]
    if package.get("registryType") != "oci":
        raise VersionGateError("MCP Registry package is not OCI")
    identifier = package.get("identifier")
    if not isinstance(identifier, str) or "@" in identifier:
        raise VersionGateError("MCP Registry OCI image tag is invalid")
    repository, separator, tag = identifier.rpartition(":")
    if not separator or "/" not in repository or any(ch.isspace() for ch in identifier):
        raise VersionGateError("MCP Registry OCI identifier is invalid")
    return (
        _valid_version(registry.get("version")),
        _valid_version(package.get("version")),
        _valid_version(tag),
    )


def collect_versions(root: Path) -> dict[str, str]:
    """Read ten independent release-version declarations."""
    core = _read_toml(root / "pyproject.toml")
    bridge = _read_toml(root / "agent_bridge" / "pyproject.toml")
    native = _read_toml(root / "native" / "windows" / "Cargo.toml")
    plugin = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    if not isinstance(plugin, dict):
        raise VersionGateError("invalid plugin metadata")
    registry_root, registry_package, registry_tag = _registry_versions(root / "server.json")

    result = {
        "core_project": _valid_version(core.get("project", {}).get("version")),
        "core_lock": _package_version(root / "uv.lock", "serverfs-mcp"),
        "bridge_project": _valid_version(bridge.get("project", {}).get("version")),
        "bridge_lock": _package_version(root / "agent_bridge" / "uv.lock", "serverfs-agent-bridge"),
        "windows_native_project": _valid_version(native.get("package", {}).get("version")),
        "windows_native_lock": _package_version(
            root / "native" / "windows" / "Cargo.lock", "serverfs-windows-native"
        ),
        "plugin": _valid_version(plugin.get("version")),
        "registry_root": registry_root,
        "registry_oci_package": registry_package,
        "registry_oci_tag": registry_tag,
    }
    return {key: result[key] for key in SOURCES}


def audit(versions: dict[str, str], *, expected: str) -> tuple[bool, bool]:
    """Coherence and release readiness are distinct, and must not be conflated."""
    _valid_version(expected)
    if set(versions) != set(SOURCES):
        raise VersionGateError("version source set incomplete")
    values = [_valid_version(versions[key]) for key in SOURCES]
    coherent = len(set(values)) == 1
    return coherent, coherent and values[0] == expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--expected", default=RELEASE_VERSION)
    args = parser.parse_args()

    try:
        versions = collect_versions(args.root)
        coherent, ready = audit(versions, expected=args.expected)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ):
        print("WINDOWS_RELEASE_VERSION_GATE=FAILED")
        return 2

    for name, version in versions.items():
        print(f"WINDOWS_VERSION_{name.upper()}={version}")
    print(f"WINDOWS_VERSION_COHERENT={'PASS' if coherent else 'FAILED'}")
    print(f"WINDOWS_RELEASE_VERSION_GATE={'PASS' if ready else 'BLOCKED'}")
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Validate all stable-release version metadata using only the Python standard library."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
OCI_REPOSITORY = "ghcr.io/ntlx/serverfs_mcp"


class MetadataError(ValueError):
    """Release metadata is missing, malformed or inconsistent."""


def validate(root: Path, tag: str) -> dict[str, str]:
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise MetadataError(f"malformed release tag: {tag!r}")
    expected = ".".join(match.groups())

    def toml(relative: str) -> dict:
        try:
            return tomllib.loads((root / relative).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise MetadataError(f"cannot read {relative}: {exc}") from exc

    def json_file(relative: str) -> dict:
        try:
            return json.loads((root / relative).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MetadataError(f"cannot read {relative}: {exc}") from exc

    def require_version(value: object, label: str) -> None:
        if value != expected:
            raise MetadataError(f"{label}: expected {expected!r}, got {value!r}")

    versions = {
        "root": toml("pyproject.toml")["project"]["version"],
        "bridge": toml("agent_bridge/pyproject.toml")["project"]["version"],
        "windows-native": toml("native/windows/Cargo.toml")["package"]["version"],
        "plugin": json_file("plugin.json")["version"],
    }
    registry = json_file("server.json")
    require_version(registry["version"], "server.json version")
    packages = registry["packages"]
    if not isinstance(packages, list) or not packages:
        raise MetadataError("server.json packages must be a non-empty list")
    oci_packages = [p for p in packages if isinstance(p, dict) and p.get("registryType") == "oci"]
    if len(oci_packages) != len(packages):
        raise MetadataError("server.json packages contain a missing or unsupported package")
    for index, package in enumerate(oci_packages):
        require_version(package.get("version"), f"server.json packages[{index}].version")
        identifier = package.get("identifier")
        if identifier != f"{OCI_REPOSITORY}:{expected}":
            raise MetadataError(
                f"server.json packages[{index}].identifier: expected "
                f"{OCI_REPOSITORY}:{expected!s}, got {identifier!r}"
            )

    lock_records = (
        ("uv.lock", "serverfs-mcp"),
        ("agent_bridge/uv.lock", "serverfs-agent-bridge"),
    )
    for relative, name in lock_records:
        lock = toml(relative)
        records = [p for p in lock.get("package", []) if p.get("name") == name]
        if len(records) != 1:
            raise MetadataError(
                f"{relative} must contain exactly one local package record {name!r}"
            )
        require_version(records[0].get("version"), f"{relative} package {name}")
        if records[0].get("source") != {"editable": "."}:
            raise MetadataError(f"{relative} package {name} is not the local editable package")

    cargo_lock = toml("native/windows/Cargo.lock")
    records = [
        p for p in cargo_lock.get("package", []) if p.get("name") == "serverfs-windows-native"
    ]
    if len(records) != 1:
        raise MetadataError(
            "native/windows/Cargo.lock must contain exactly one local package record"
        )
    require_version(records[0].get("version"), "native/windows/Cargo.lock serverfs-windows-native")

    for label, version in versions.items():
        require_version(version, label)
    return {**versions, "registry": registry["version"], "tag": tag}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    try:
        versions = validate(args.root, args.tag)
    except (MetadataError, KeyError, TypeError, AttributeError, OSError) as exc:
        parser.error(str(exc))
    print("release metadata consistent:", versions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

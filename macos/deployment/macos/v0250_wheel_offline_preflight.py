"""Read-only inventory of wheel requirements in installed environments and a wheel cache."""

from __future__ import annotations

import argparse
import importlib.metadata
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def _requirements(wheel: Path) -> list[Requirement]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_paths = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise ValueError(f"{wheel}: expected one .dist-info/METADATA")
        metadata = Parser().parsestr(archive.read(metadata_paths[0]).decode("utf-8"))
        return [Requirement(value) for value in metadata.get_all("Requires-Dist", [])]


def _installed(path: Path) -> dict[str, set[Version]]:
    found: dict[str, set[Version]] = {}
    if not path.is_dir():
        return found
    for distribution in importlib.metadata.distributions(path=[str(path)]):
        name = distribution.metadata.get("Name")
        if name:
            found.setdefault(canonicalize_name(name), set()).add(Version(distribution.version))
    return found


def _cached(path: Path) -> dict[str, set[Version]]:
    found: dict[str, set[Version]] = {}
    if not path.is_dir():
        return found
    for wheel in path.rglob("*.whl"):
        try:
            with zipfile.ZipFile(wheel) as archive:
                paths = [
                    name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
                ]
                if len(paths) != 1:
                    continue
                metadata = Parser().parsestr(archive.read(paths[0]).decode("utf-8"))
                name, version = metadata.get("Name"), metadata.get("Version")
                if name and version:
                    found.setdefault(canonicalize_name(name), set()).add(Version(version))
        except (OSError, UnicodeError, zipfile.BadZipFile, KeyError, ValueError):
            continue
    return found


def _satisfies(requirement: Requirement, versions: set[Version]) -> bool:
    return any(version in requirement.specifier for version in versions)


def inspect_wheel(
    wheel: Path, environment: Path, cache: Path, *, extras: set[str] | None = None
) -> list[tuple[str, bool, bool]]:
    installed = _installed(environment)
    cached = _cached(cache)
    extras = extras or set()
    results = []
    for requirement in _requirements(wheel):
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            if not any(requirement.marker.evaluate({"extra": extra}) for extra in extras):
                continue
        name = canonicalize_name(requirement.name)
        results.append(
            (
                str(requirement),
                _satisfies(requirement, installed.get(name, set())),
                _satisfies(requirement, cached.get(name, set())),
            )
        )
    return results


def _label(requirement: str, installed: bool, cached: bool) -> str:
    if installed and cached:
        return "installed-and-cached"
    if installed:
        return "installed-only"
    if cached:
        return "cache-only"
    return "missing-from-environment-and-cache"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-wheel", required=True, type=Path)
    parser.add_argument("--bridge-wheel", required=True, type=Path)
    parser.add_argument("--root-env", required=True, type=Path)
    parser.add_argument("--bridge-env", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overall = 0
    for label, wheel, environment in (
        ("root", args.root_wheel, args.root_env),
        ("bridge", args.bridge_wheel, args.bridge_env),
    ):
        try:
            results = inspect_wheel(wheel, environment, args.cache)
        except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
            print(f"{label}: cannot inspect wheel ({exc})", file=sys.stderr)
            return 2
        print(f"{label}: {wheel.name}")
        for requirement, installed, cached in results:
            status = _label(requirement, installed, cached)
            print(f"  {requirement}: {status}")
            if not installed and not cached:
                overall = 1
    print("Inventory only: cache metadata is not an installation or integrity proof.")
    return overall


if __name__ == "__main__":
    raise SystemExit(main())

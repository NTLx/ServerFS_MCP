"""Rehearse removal of legacy root product paths without modifying the checkout.

Archive an explicit candidate commit, omit only reviewed retirement categories,
and verify the remaining product distribution in an external temporary tree.
The inventory is a review aid, not authorization to delete source.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = ("linux", "macos", "windows")
LEGACY_FILES = {
    ".dockerignore",
    ".env.example",
    "Dockerfile",
    "compose.agent.yml",
    "compose.cloudflare.yml",
    "compose.http.yml",
    "compose.oauth.yml",
    "compose.yml",
    "pyproject.toml",
    "serverfs.toml.example",
    "uv.lock",
    "deployment/conformance_target.py",
    "deployment/native/proxy_acceptance_harness.py",
}
LEGACY_PREFIXES = (
    "src/",
    "agent_bridge/",
    "native/",
    "deployment/agent-bridge/",
    "deployment/linux/",
    "deployment/macos/",
    "deployment/tunnel/",
    "deployment/windows/",
)
COMMON_TESTS = {
    "tests/test_agent_skills.py",
    "tests/test_registry_manifest.py",
    "tests/test_release_metadata.py",
    "tests/test_release_workflow.py",
    "tests/test_wheel_release.py",
    "tests/e2e/test_v026_site_static_acceptance.py",
    "tests/e2e/v026_site_static_acceptance.py",
}


def retired(name: str) -> bool:
    return (
        name in LEGACY_FILES
        or name.startswith(LEGACY_PREFIXES)
        or (name.startswith("tests/") and name not in COMMON_TESTS)
    )


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], timeout=60)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", help="Exact candidate commit SHA")
    args = parser.parse_args()
    revision = git("rev-parse", "--verify", args.revision + "^{commit}").decode().strip()
    if args.revision != revision:
        parser.error("supply the full immutable commit SHA")
    names = set(
        filter(None, git("ls-tree", "-r", "--name-only", "-z", revision).decode().split("\0"))
    )
    omitted = {name for name in names if retired(name)}
    retained = names - omitted
    if not omitted:
        raise RuntimeError("expected legacy paths for retirement rehearsal")
    if not COMMON_TESTS <= retained:
        raise RuntimeError("common distribution test inventory is incomplete")
    for platform in PLATFORMS:
        required = {
            f"{platform}/pyproject.toml",
            f"{platform}/uv.lock",
            f"{platform}/agent_bridge/pyproject.toml",
            f"{platform}/agent_bridge/uv.lock",
        }
        if not required <= retained:
            raise RuntimeError(f"missing platform build roots: {platform}")
    with tempfile.TemporaryDirectory(prefix="sf27-retirement-") as temp:
        scratch = Path(temp).resolve()
        if scratch.is_relative_to(ROOT):
            raise RuntimeError("rehearsal must be outside the checkout")
        archive = scratch / "candidate.tar"
        tree = scratch / "repo"
        tree.mkdir()
        with archive.open("xb") as output:
            subprocess.run(
                ["git", "-C", str(ROOT), "archive", "--format=tar", revision],
                stdout=output,
                check=True,
                timeout=120,
            )
        with tarfile.open(archive) as bundle:
            for member in bundle:
                name = PurePosixPath(member.name).as_posix()
                if name not in retained:
                    continue
                if not member.isfile():
                    raise RuntimeError(f"nonregular retained archive member: {name}")
                bundle.extract(member, tree, filter="data")
        actual = {
            p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()
        }
        if actual != retained:
            raise RuntimeError("retirement snapshot does not match retained Git inventory")
        # The independent gate explicitly refuses imports of all product packages.
        subprocess.run(
            [
                sys.executable, "-I", "-B",
                str(tree / "deployment/native/check_distribution.py"),
            ],
            cwd=tree,
            check=True,
            timeout=180,
        )
        missing = sorted(
            name for name in omitted
            if not any(f"{platform}/{name}" in retained for platform in PLATFORMS)
        )
        print(json.dumps({
            "revision": revision,
            "legacy_files_omitted": len(omitted),
            "retained_files": len(retained),
            "common_test_files": len(COMMON_TESTS),
            "omitted_without_same_relative_platform_path": missing,
            "status": "rehearsal_pass",
            "actual_retirement_performed": False,
        }, indent=2), flush=True)


if __name__ == "__main__":
    main()

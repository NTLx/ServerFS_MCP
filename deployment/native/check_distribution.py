"""Common release/distribution tests, with no product runtime import allowed."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = (
    "tests/test_agent_skills.py",
    "tests/test_registry_manifest.py",
    "tests/test_release_metadata.py",
    "tests/test_release_workflow.py",
    "tests/test_wheel_release.py",
    "tests/e2e/test_v026_site_static_acceptance.py",
)
PRODUCTS = ("serverfs_mcp", "serverfs_agent_bridge", "serverfs_windows_native")


def main() -> int:
    # Fail on accidental dependency on an installed/editable product, including
    # a legacy root conftest. Pytest and jsonschema are external tooling only.
    if any(name.split(".", 1)[0] in PRODUCTS for name in sys.modules):
        raise RuntimeError("distribution validation started with product code loaded")
    for package in PRODUCTS:
        sys.modules[package] = None
    import pytest

    return pytest.main(
        ["--noconftest", "-q", "-o", "addopts=", "--rootdir", str(ROOT),
         *(str(ROOT / name) for name in TESTS)]
    )


if __name__ == "__main__":
    raise SystemExit(main())

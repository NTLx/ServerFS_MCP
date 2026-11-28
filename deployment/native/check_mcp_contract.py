"""External comparison against the immutable v0.26.0 public MCP tool baseline."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import runpy
import sys
from pathlib import Path

BASELINE_SHA256 = "e4aff42ebcf40a587912b9d0832a74e263dc5f2ee28322feee296639a96aea4d"
PLATFORMS = {"linux": "linux", "darwin": "macos", "win32": "windows"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    args = parser.parse_args()
    repository = args.repository.resolve(strict=True)
    platform = PLATFORMS[sys.platform]
    baseline_path = repository / "contracts/v0.26.0/mcp-tools.json"
    # Normalize only checkout newline conversion, not JSON structure or fields.
    raw = baseline_path.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(raw).hexdigest() != BASELINE_SHA256:
        raise SystemExit("immutable v0.26.0 contract digest mismatch")
    expected = json.loads(raw)
    import serverfs_mcp

    origin = Path(serverfs_mcp.__file__).resolve(strict=True)
    if not origin.is_relative_to(repository / platform / "src"):
        raise SystemExit("Core import is not from the owning platform candidate")
    extractor = runpy.run_path(str(repository / "docs/diagnostics/v0270-capture-v026-contract.py"))
    actual = asyncio.run(extractor["capture"]())
    for key in ("mcp_sdk_version", "format_version", "variants"):
        if actual[key] != expected[key]:
            raise SystemExit(f"{platform}: contract mismatch: {key}")
    mismatches = [
        name
        for name in sorted(expected["definitions"].keys() | actual["definitions"].keys())
        if actual["definitions"].get(name) != expected["definitions"].get(name)
    ]
    if mismatches:
        raise SystemExit(f"{platform}: tool definition mismatch: {', '.join(mismatches)}")
    print(
        f"CONTRACT_PASS {platform} variants={len(actual['variants'])} "
        f"tools={len(actual['definitions'])} sdk={actual['mcp_sdk_version']}"
    )


if __name__ == "__main__":
    main()

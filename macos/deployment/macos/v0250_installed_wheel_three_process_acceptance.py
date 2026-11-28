"""Run the installed-wheel three-process macOS acceptance in private scratch."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--public-hold-seconds", type=int)
    parser.add_argument("--historical-bridge-rollback", action="store_true")
    args = parser.parse_args()
    if args.public_hold_seconds is not None and not 1 <= args.public_hold_seconds <= 180:
        parser.error("--public-hold-seconds must be between 1 and 180")
    deployment = Path(__file__).resolve().parent
    repository = deployment.parents[1]
    wheelhouse = args.wheelhouse.resolve(strict=True)
    if not wheelhouse.is_dir() or not all(
        (wheelhouse / name / "bin/python").is_file() for name in ("core-venv", "bridge-venv")
    ):
        parser.error("--wheelhouse must contain core-venv/bin/python and bridge-venv/bin/python")
    if not wheelhouse.is_relative_to(repository / "dist"):
        parser.error("--wheelhouse must be under the platform-local dist directory")
    if args.historical_bridge_rollback:
        if args.public_hold_seconds is not None:
            parser.error("historical rollback is a local-only fixture")
        if not (wheelhouse / "baseline-bridge-venv/bin/python").is_file():
            parser.error("historical rollback requires an installed baseline Bridge environment")
    # Leave space for /case/owner.sock within the Darwin sockaddr_un budget.
    scratch = Path(tempfile.mkdtemp(prefix=".sf-wheel-", dir=deployment))
    scratch.chmod(0o700)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHONPATH", "SERVERFS_", "CONTROL_PLANE_"))
    }
    env.update(
        SERVERFS_V0250_WHEEL_SCRATCH=str(scratch),
        SERVERFS_V0250_WHEELHOUSE=str(wheelhouse),
        SERVERFS_ENABLE_INSTALLED_WHEEL_E2E="1",
    )
    if args.public_hold_seconds is not None:
        env["SERVERFS_V0250_CF_HOLD_SECONDS"] = str(args.public_hold_seconds)
    if args.historical_bridge_rollback:
        env["SERVERFS_V27_HISTORICAL_BRIDGE_ROLLBACK"] = "1"
    try:
        test = repository / "tests/test_darwin_v0250_installed_wheel_three_process_macos.py"
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "pytest", "-q", "-s", str(test)],
            cwd=scratch,
            env=env,
            check=False,
        )
        print(f"scratch={scratch}")
        print(f"pytest_exit={result.returncode}")
        return result.returncode
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

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
    parser.add_argument("--public-hold-seconds", type=int)
    args = parser.parse_args()
    if args.public_hold_seconds is not None and not 1 <= args.public_hold_seconds <= 180:
        parser.error("--public-hold-seconds must be between 1 and 180")
    deployment = Path(__file__).resolve().parent
    repository = deployment.parents[1]
    scratch = Path(tempfile.mkdtemp(prefix=".serverfs-v0250-wheel-e2e-", dir=deployment))
    scratch.chmod(0o700)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHONPATH", "SERVERFS_", "CONTROL_PLANE_"))
    }
    env.update(
        SERVERFS_V0250_WHEEL_SCRATCH=str(scratch),
        SERVERFS_V0250_WHEELHOUSE=str(deployment / ".serverfs-v0250-wheelhouse-CusylLCY"),
        SERVERFS_ENABLE_INSTALLED_WHEEL_E2E="1",
    )
    if args.public_hold_seconds is not None:
        env["SERVERFS_V0250_CF_HOLD_SECONDS"] = str(args.public_hold_seconds)
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

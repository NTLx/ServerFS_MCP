"""Run historical installed Bridge rollback as processes or transient systemd units."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

BASELINE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
REPO = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--systemd", action="store_true", help="Require a clean user manager")
    parser.add_argument("--online", action="store_true", help="Allow dependency downloads")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("Linux only")
    uv = Path(shutil.which("uv") or (Path.home() / ".local/bin/uv"))
    if not uv.is_absolute() or not uv.is_file() or not os.access(uv, os.X_OK):
        raise RuntimeError("uv unavailable in PATH or ~/.local/bin")
    project = REPO / "linux/agent_bridge"
    python = project / ".venv/bin/python"
    ruff = project / ".venv/bin/ruff"
    test = "tests/test_linux_v0250_provision.py"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON", "UV_PROJECT"))
        and key not in {"VIRTUAL_ENV", "CONDA_PREFIX", "UV_OFFLINE"}
    }
    env.update(
        PATH=str(uv.parent) + os.pathsep + env.get("PATH", os.defpath),
        UV_PYTHON=str(Path(sys._base_executable).resolve(strict=True)),
        SERVERFS_LINUX_INSTALL_TEST="online" if args.online else "offline",
        SERVERFS_LINUX_INSTALL_SYSTEMD_ACCEPT="1" if args.systemd else "0",
        SERVERFS_LINUX_SYSTEMD_TEST="0",
    )
    if not args.online:
        env["UV_OFFLINE"] = "1"
    subprocess.run([str(ruff), "check", test], cwd=project, env=env, check=True)
    subprocess.run([str(ruff), "format", "--check", test], cwd=project, env=env, check=True)
    with tempfile.TemporaryDirectory(prefix="sf27-release-") as temp:
        scratch = Path(temp)
        archive = scratch / "baseline.tar"
        with archive.open("xb") as output:
            subprocess.run(
                ["git", "-C", str(REPO), "archive", BASELINE, "agent_bridge"],
                stdout=output, check=True, timeout=60,
            )
        with tarfile.open(archive) as bundle:
            bundle.extractall(scratch, filter="data")
        baseline = scratch / "agent_bridge"
        if not (baseline / "pyproject.toml").is_file():
            raise RuntimeError("historical Bridge archive is incomplete")
        env["SERVERFS_LINUX_BASELINE_BRIDGE_SOURCE"] = str(baseline)
        case = (
            "test_actual_release_systemd_upgrade_rollback"
            if args.systemd
            else "test_actual_release_packaging"
        )
        report = scratch / "rollback.xml"
        subprocess.run(
            [
                str(python), "-m", "pytest", "-q", "-rs", "-o", "addopts=",
                f"{test}::{case}", f"--junitxml={report}",
            ],
            cwd=project,
            env=env,
            check=True,
            timeout=600,
        )
        cases = list(ET.parse(report).iter("testcase"))
        if (
            len(cases) != 1
            or cases[0].get("name") != case
            or any(
                cases[0].find(outcome) is not None for outcome in ("failure", "error", "skipped")
            )
        ):
            raise RuntimeError("required historical rollback test did not execute successfully")
    mode = "SYSTEMD" if args.systemd else "PROCESS"
    print(f"LINUX_{mode}_ROLLBACK_PASS baseline={BASELINE}", flush=True)


if __name__ == "__main__":
    main()

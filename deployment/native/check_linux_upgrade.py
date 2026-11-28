"""Run real v0.26.0 -> split Bridge -> v0.26.0 process rollback in private scratch."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BASELINE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
REPO = Path(__file__).resolve().parents[2]


def main() -> None:
    if sys.platform != "linux":
        raise SystemExit("Linux only")
    project = REPO / "linux/agent_bridge"
    python = project / ".venv/bin/python"
    ruff = project / ".venv/bin/ruff"
    test = "tests/test_linux_v0250_provision.py"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON", "UV_PROJECT"))
        and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    env.update(
        UV_OFFLINE="1",
        UV_PYTHON=str(Path(sys._base_executable).resolve(strict=True)),
        SERVERFS_LINUX_INSTALL_TEST="offline",
        SERVERFS_LINUX_INSTALL_SYSTEMD_ACCEPT="0",
        SERVERFS_LINUX_SYSTEMD_TEST="0",
    )
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
        subprocess.run(
            [str(python), "-m", "pytest", "-q", "-o", "addopts=",
             test + "::test_actual_release_packaging"],
            cwd=project, env=env, check=True, timeout=600,
        )
    print(f"LINUX_PROCESS_ROLLBACK_PASS baseline={BASELINE}", flush=True)


if __name__ == "__main__":
    main()

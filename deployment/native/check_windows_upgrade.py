"""Run the controller-authored installed rollback test against the pinned release source."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "windows"
BASELINE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
REPORT = ROOT / "dist/v0270-installed-rollback-result.json"
FILES = [
    "tests/test_windows_v25_bridge_v2_real_worker.py",
    "tests/test_windows_v27_installed_bridge_rollback.py",
]
data = {"status": "running", "baseline": BASELINE, "steps": []}


def save():
    temp = REPORT.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(REPORT)


def run(name, argv, env, *, cwd=ROOT):
    completed = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    data["steps"].append({
        "name": name, "exit": completed.returncode,
        "output": (completed.stdout + "\n" + completed.stderr)[-20000:],
    })
    if completed.returncode:
        data["status"] = "failed"
    save()
    print(f"{name}: {completed.returncode}", flush=True)
    if completed.returncode:
        print(data["steps"][-1]["output"][-4000:], flush=True)
        sys.exit(completed.returncode)


def main():
    if sys.platform != "win32":
        raise SystemExit("Requires native windows")
    (ROOT / "dist").mkdir(exist_ok=True)
    save()
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON", "UV_PROJECT"))
        and k.upper() not in {"VIRTUAL_ENV", "CONDA_PREFIX", "UV_WORKING_DIR"}
    }
    env["UV_OFFLINE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    sys.stdout.reconfigure(errors="backslashreplace")
    ruff = ROOT / ".venv/Scripts/ruff.exe"
    python = ROOT / ".venv/Scripts/python.exe"
    run("lint", [str(ruff), "check", *FILES], env)
    run("format", [str(ruff), "format", "--diff", *FILES], env)
    with tempfile.TemporaryDirectory(prefix="sf27-win-rollback-") as temp:
        scratch = Path(temp).resolve()
        archive = scratch / "historical.tar"
        with archive.open("xb") as output:
            subprocess.run(
                ["git", "-C", str(ROOT.parent), "archive", BASELINE, "agent_bridge"],
                stdout=output, check=True, timeout=60,
            )
        with tarfile.open(archive) as bundle:
            bundle.extractall(scratch, filter="data")
        env["SERVERFS_WINDOWS_BASELINE_BRIDGE_SOURCE"] = str(scratch / "agent_bridge")
        env["SERVERFS_WINDOWS_INSTALLED_ROLLBACK"] = "1"
        env["SERVERFS_DATA_HOME"] = str(scratch / "data")
        run("installed-rollback", [str(python), "-m", "pytest", "-q", "-s", "-o", "addopts=",
                                   FILES[1]], env)
        run("worker-regression", [str(python), "-m", "pytest", "-q", "-o", "addopts=",
                                  FILES[0], "tests/test_windows_v25_two_independent_bridge_workers.py",
                                  "tests/test_windows_v25_topology_c_real_core_processes.py"], env)
    data["status"] = "passed"
    save()
    print("WINDOWS_INSTALLED_ROLLBACK_PASS", flush=True)


if __name__ == "__main__":
    main()

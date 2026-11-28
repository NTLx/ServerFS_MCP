"""Build pinned historical and candidate wheels and run private macOS rollback acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "macos"
BASELINE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
REPORT = ROOT / "dist/v0270-historical-rollback-result.json"
FILES = [
    "tests/test_darwin_v0250_installed_wheel_three_process_macos.py",
    "deployment/macos/v0250_installed_wheel_three_process_acceptance.py",
]
data = {"status": "running", "baseline": BASELINE, "steps": [], "wheels": []}


def save():
    temp = REPORT.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(REPORT)


def run(name, argv, env, *, cwd=ROOT):
    result = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    data["steps"].append({
        "name": name, "exit": result.returncode,
        "output": (result.stdout + "\n" + result.stderr)[-16000:],
    })
    if result.returncode:
        data["status"] = "failed"
    save()
    print(f"{name}: {result.returncode}", flush=True)
    if result.returncode:
        print(data["steps"][-1]["output"][-3000:], flush=True)
        sys.exit(result.returncode)


def main():
    if sys.platform != "darwin":
        raise SystemExit("Requires native macOS")
    (ROOT / "dist").mkdir(exist_ok=True)
    save()
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv unavailable")
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON", "UV_PROJECT"))
        and k not in {"VIRTUAL_ENV", "CONDA_PREFIX", "UV_WORKING_DIR"}
    }
    env["UV_OFFLINE"] = "1"
    ruff = ROOT / ".venv/bin/ruff"
    run("lint", [str(ruff), "check", *FILES], env)
    run("format", [str(ruff), "format", "--diff", *FILES], env)
    with tempfile.TemporaryDirectory(prefix="sf27-mac-history-") as historical:
        history = Path(historical).resolve()
        archive = history / "baseline.tar"
        with archive.open("xb") as output:
            subprocess.run(
                ["git", "-C", str(ROOT.parent), "archive", BASELINE, "agent_bridge"],
                stdout=output, check=True, timeout=60,
            )
        with tarfile.open(archive) as bundle:
            bundle.extractall(history, filter="data")
        wheelhouse = Path(tempfile.mkdtemp(prefix="v0270-historical-", dir=ROOT / "dist"))
        data["wheelhouse"] = wheelhouse.name
        save()
        for name, source, package in (
            ("baseline-bridge", history / "agent_bridge", "serverfs_agent_bridge"),
            ("core", ROOT, "serverfs_mcp"),
            ("bridge", ROOT / "agent_bridge", "serverfs_agent_bridge"),
        ):
            wheels = wheelhouse / (name + "-wheels")
            requirements = wheelhouse / (name + ".txt")
            venv = wheelhouse / (name + "-venv")
            python = venv / "bin/python"
            run(name + "-build", [uv, "build", "--offline", "--wheel", "--out-dir",
                                  str(wheels)], env, cwd=source)
            built = list(wheels.glob(package + "-0.26.0-*.whl"))
            assert len(built) == 1
            wheel = built[0]
            data["wheels"].append({
                "name": wheel.name, "role": name,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            })
            save()
            run(name + "-lock", [uv, "export", "--offline", "--locked", "--no-dev",
                "--no-emit-project", "--output-file", str(requirements)], env, cwd=source)
            run(name + "-venv", [uv, "venv", "--offline", "--no-project", "--python",
                sys._base_executable, str(venv)], env, cwd=source)
            run(name + "-sync", [uv, "pip", "sync", "--offline", "--python",
                str(python), str(requirements)], env, cwd=source)
            run(name + "-install", [uv, "pip", "install", "--offline", "--no-deps",
                "--python", str(python), str(wheel)], env, cwd=source)
        run("native-process-rollback", [
            str(ROOT / ".venv/bin/python"), "-I", "-B",
            "deployment/macos/v0250_installed_wheel_three_process_acceptance.py",
            "--wheelhouse", str(wheelhouse), "--historical-bridge-rollback",
        ], env)
    data["status"] = "passed"
    save()
    print("MACOS_HISTORICAL_ROLLBACK_PASS", flush=True)


if __name__ == "__main__":
    main()

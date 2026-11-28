"""Build Linux Core and Bridge wheels from independent minimal source contexts.

The scratch directory lives under the candidate's ignored virtualenv, not in
the old runtime or a production install. This proves build inputs and wheel
identity, not absence of residual foreign code or native deployment parity.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "linux"
OUT = ROOT / "docs/diagnostics/v0270-linux-wheel-build.json"


def run(args, cwd):
    proc = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    print(proc.stdout, end="")
    print(proc.stderr, end="")
    if proc.returncode:
        raise SystemExit(proc.returncode)


def build(source, scratch, package):
    scratch.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
        path = source / name
        if path.is_file():
            shutil.copy2(path, scratch / name)
    shutil.copytree(source / "src", scratch / "src", ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", "*.pyo"
    ))
    manifest = tomllib.loads((scratch / "pyproject.toml").read_text())
    run(["uv", "lock", "--check", "--offline"], scratch)
    run(["uv", "build", "--offline", "--wheel", "--out-dir", "dist"], scratch)
    wheels = list((scratch / "dist").glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert package + "/__init__.py" in names
        assert all(not name.startswith(("native/macos/", "windows/", "macos/")) for name in names)
        assert all(not name.endswith((".pyc", ".pyo")) for name in names)
        assert archive.testzip() is None
        metadata = next(name for name in names if name.endswith(".dist-info/METADATA"))
        text = archive.read(metadata).decode()
        assert f"Version: {manifest['project']['version']}\n" in text
        assert f"Name: {manifest['project']['name']}\n" in text
    return {"package": package, "wheel": wheel.name,
            "version": manifest["project"]["version"],
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "members": len(names), "lock_check": "pass", "wheel_build": "pass"}


def main():
    assert not OUT.exists()
    assert (PLATFORM / ".venv").is_dir()
    with tempfile.TemporaryDirectory(prefix="split-wheel-", dir=PLATFORM / ".venv") as path:
        scratch = Path(path)
        reports = [
            build(PLATFORM, scratch / "core", "serverfs_mcp"),
            build(PLATFORM / "agent_bridge", scratch / "bridge", "serverfs_agent_bridge"),
        ]
    result = {"scope": "Linux minimal independent build contexts, offline",
              "results": reports,
              "limitations": "Not Docker image, installed wheel, full test suite, or native three-platform acceptance."}
    with OUT.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    print("PASS: both Linux candidate wheels built from minimal independent sources")


if __name__ == "__main__":
    main()

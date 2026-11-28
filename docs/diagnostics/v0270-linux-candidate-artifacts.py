"""Offline Linux candidate artifacts and frozen MCP registration parity.

Build only the minimal local source inputs, extract the wheel in temporary
scratch, and use the existing locked test interpreters to import its actual
code. Do not install a wheel, touch production, or run a provider.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "linux"
OUT = ROOT / "docs/diagnostics/v0270-linux-candidate-artifacts.json"
CAPTURE = ROOT / "docs/diagnostics/v0270-capture-v026-contract.py"
FROZEN = ROOT / "contracts/v0.26.0/mcp-tools.json"
FOREIGN_MODULES = {
    "agent_core_grant_client.py", "cloudflare_bootstrap.py",
    "native_lifecycle.py", "supervisor.py", "protocol_windows_native.py",
    "repo_ops_windows.py", "acp_process_windows.py", "codex_windows.py",
    "macos_owned_config.py", "platform_seams.py",
}


def run(argv, cwd):
    completed = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    if completed.returncode:
        print(completed.stdout, end="")
        print(completed.stderr, end="", file=sys.stderr)
        raise SystemExit(completed.returncode)
    return completed.stdout


def foreign_member(name):
    path = PurePosixPath(name)
    if len(path.parts) < 2 or path.parts[0] not in {
        "serverfs_mcp", "serverfs_agent_bridge"
    }:
        return False
    return any(part.startswith(("windows_", "darwin_"))
               or part in FOREIGN_MODULES for part in path.parts[1:])


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
    extracted = scratch / "wheel"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert package + "/__init__.py" in names
        assert all(not foreign_member(name) for name in names), [
            name for name in names if foreign_member(name)
        ]
        assert not any(name.endswith((".pyc", ".pyo")) for name in names)
        assert archive.testzip() is None
        metadata = next(name for name in names if name.endswith(".dist-info/METADATA"))
        text = archive.read(metadata).decode()
        assert f"Version: {manifest['project']['version']}\n" in text
        assert f"Name: {manifest['project']['name']}\n" in text
        archive.extractall(extracted)
    return extracted, {
        "package": package, "wheel": wheel.name,
        "version": manifest["project"]["version"],
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "members": len(names), "lock_check": "pass", "wheel_build": "pass",
        "foreign_named_runtime_members": 0,
    }


def main():
    assert not OUT.exists()
    bridge_python = PLATFORM / "agent_bridge/.venv/bin/python"
    assert bridge_python.is_file()
    with tempfile.TemporaryDirectory(prefix="candidate-artifacts-", dir=PLATFORM / ".venv") as tmp:
        scratch = Path(tmp)
        core_path, core = build(PLATFORM, scratch / "core", "serverfs_mcp")
        bridge_path, bridge = build(PLATFORM / "agent_bridge", scratch / "bridge", "serverfs_agent_bridge")
        # The historical capture function only registers tools in a scratch
        # workdir; its baseline main() and write path are deliberately unused.
        core_probe = """
import asyncio, json, runpy, sys
from pathlib import Path
wheel, capture, frozen = map(Path, sys.argv[1:])
sys.path.insert(0, str(wheel))
import serverfs_mcp
assert Path(serverfs_mcp.__file__).is_relative_to(wheel)
result = asyncio.run(runpy.run_path(str(capture))["capture"]())
baseline = json.loads(frozen.read_text())
for key in ("variants", "definitions", "mcp_sdk_version"):
    assert result[key] == baseline[key], f"frozen contract mismatch: {key}"
print(json.dumps({"variants": len(result["variants"]),
                  "tools": len(result["definitions"]),
                  "sdk": result["mcp_sdk_version"],
                  "exact_schema_parity": True}))
"""
        core["extracted_wheel_contract"] = json.loads(run(
            [sys.executable, "-I", "-c", core_probe, str(core_path), str(CAPTURE), str(FROZEN)],
            scratch,
        ))
        bridge_probe = """
import json, sys
from pathlib import Path
wheel = Path(sys.argv[1])
sys.path.insert(0, str(wheel))
import serverfs_agent_bridge
from serverfs_agent_bridge import main, protocol, store, repo_ops, inspect_state
assert all(Path(mod.__file__).is_relative_to(wheel) for mod in
           (serverfs_agent_bridge, main, protocol, store, repo_ops, inspect_state))
print(json.dumps({"native_imports": "pass"}))
"""
        bridge["extracted_wheel_import"] = json.loads(run(
            [str(bridge_python), "-I", "-c", bridge_probe, str(bridge_path)], scratch,
        ))
    result = {
        "scope": "Linux independent offline wheel contexts and execution/import from extracted archives",
        "results": [core, bridge],
        "limitations": [
            "Wheel code executed with locked existing dependencies, not a clean installation.",
            "Foreign member check identifies known module names; not a proof of all residual branches.",
            "No Docker image, native host deployment, provider, or full Gate A acceptance.",
        ],
    }
    with OUT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

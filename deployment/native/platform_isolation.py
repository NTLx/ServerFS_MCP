"""External Gate A verification of a platform-only source tree.

Copy only Git-tracked files (using their current working-tree contents) plus
explicit new build hooks. Never copy ignored local config, venvs or credentials.
Build and install in a fresh temporary directory outside the checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PLATFORMS = {"linux": "linux", "darwin": "macos", "win32": "windows"}
NEW_SOURCE = {"src/hatch_build.py", "agent_bridge/src/hatch_build.py"}


def run(args: list[str], cwd: Path, env: dict[str, str]) -> None:
    print(f"RUN {Path(args[0]).name} {args[1:]} (cwd={cwd.name})", flush=True)
    subprocess.run(args, cwd=cwd, env=env, check=True, timeout=600)


def snapshot(source: Path, target: Path) -> int:
    files = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--", "."], cwd=source
    ).decode("utf-8").split("\0")
    pending = subprocess.check_output(
        ["git", "ls-files", "-z", "--others", "--exclude-standard", "--", "."], cwd=source
    ).decode("utf-8").split("\0")
    reviewed = NEW_SOURCE | {
        "macos": {"tests/test_platform_local_agent_doctor.py"},
        "windows": {
            "deployment/windows/native-operations.md",
            "tests/test_windows_v27_installed_bridge_rollback.py",
        },
    }.get(source.name, set())
    unexpected = set(filter(None, pending)) - reviewed
    if unexpected:
        raise RuntimeError(f"unreviewed new platform files: {sorted(unexpected)}")
    files = sorted(set(filter(None, files)) | set(filter(None, pending)))
    copied = 0
    for relative in files:
        src = source / relative
        if not src.exists() and not src.is_symlink():
            continue  # reviewed working-tree deletion
        if not src.is_file() or src.is_symlink() or not src.resolve().is_relative_to(source):
            raise RuntimeError(f"nonregular or escaping source: {relative}")
        dst = target / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied


def python_in(project: Path) -> Path:
    return project / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--online", action="store_true",
        help="Allow build/dependency downloads on disposable CI runners; offline by default.",
    )
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    if source.name != PLATFORMS.get(sys.platform):
        raise RuntimeError("use the owning platform source on its native host")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv unavailable")
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON", "UV_PROJECT"))
        and k.upper() not in {
            "VIRTUAL_ENV", "UV_WORKING_DIR", "CONDA_PREFIX", "UV_OFFLINE", "CARGO_NET_OFFLINE"
        }
    }
    env["UV_PYTHON"] = str(Path(sys._base_executable).resolve(strict=True))
    offline = [] if args.online else ["--offline"]
    if not args.online:
        env.update(UV_OFFLINE="1", CARGO_NET_OFFLINE="true")
    with tempfile.TemporaryDirectory(prefix="sf27-isolated-") as temp:
        scratch = Path(temp).resolve()
        repo = Path(subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=source, text=True
        ).strip()).resolve()
        if scratch.is_relative_to(repo):
            raise RuntimeError("temporary directory must be outside the source repository")
        isolated = scratch / source.name
        print(f"ISOLATED_FILES={snapshot(source, isolated)}", flush=True)
        print("OUTSIDE_REPO=True", flush=True)
        for component, project, package in (
            ("core", isolated, "serverfs_mcp"),
            ("bridge", isolated / "agent_bridge", "serverfs_agent_bridge"),
        ):
            dist = scratch / component
            run([uv, "build", *offline, "--wheel", "--out-dir", str(dist)], project, env)
            wheels = list(dist.glob(f"{package}-*.whl"))
            if len(wheels) != 1:
                raise RuntimeError(f"expected one {component} wheel")
            wheel = wheels[0]
            with zipfile.ZipFile(wheel) as bundle:
                if bundle.testzip() is not None:
                    raise RuntimeError("corrupt wheel")
            print(f"WHEEL {wheel.name} SHA256={hashlib.sha256(wheel.read_bytes()).hexdigest()}", flush=True)
            run([uv, "sync", "--frozen", *offline, "--no-install-project"], project, env)
            python = python_in(project)
            run([uv, "pip", "install", *offline, "--no-deps", "--python", str(python), str(wheel)], project, env)
            if component == "core" and os.name == "nt":
                native = project / "native/windows"
                run([uv, "tool", "run", *offline, "--from", "maturin==1.8.2",
                     "maturin", "build", "--release", "--features", "pyo3", "--locked", *offline], native, env)
                native_wheels = list((native / "target/wheels").glob("serverfs_windows_native-*.whl"))
                if len(native_wheels) != 1:
                    raise RuntimeError("expected one isolated native wheel")
                native_wheel = native_wheels[0]
                print(f"WHEEL {native_wheel.name} SHA256={hashlib.sha256(native_wheel.read_bytes()).hexdigest()}", flush=True)
                run([uv, "pip", "install", *offline, "--no-deps", "--python", str(python), str(native_wheel)], project, env)
            probe = (
                f"import {package} as p; from pathlib import Path; import sys; "
                "assert Path(p.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())"
            )
            run([str(python), "-I", "-c", probe], project, env)
            tests = (
                ["tests/test_backends.py", "tests/test_mcp_protocol_compat.py",
                 "tests/test_mcp_http_wire_compat.py"]
                if component == "core" else ["tests/test_protocol.py", "tests/test_core_grants.py"]
            )
            if component == "core" and os.name == "nt":
                tests.append("tests/test_agent_client.py")
            run([str(python), "-I", "-m", "pytest", "-q", "-o", "addopts=", *tests], project, env)
        print("PLATFORM_ISOLATION_PASS", flush=True)


if __name__ == "__main__":
    main()

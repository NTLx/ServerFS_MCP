"""Provision a fresh named Windows v25 component runtime from pinned offline Wheels.

Does not configure, register, start, stop or repair a component. Only private
new/empty environments are accepted. A failed attempt is retained for explicit
operator review; there is no automatic destructive recovery.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

VERSION = "0.26.0"
WHEELS = {
    "core": (f"serverfs_mcp-{VERSION}-py3-none-win_amd64.whl", "serverfs-mcp", "serverfs_mcp"),
    "native": (
        f"serverfs_windows_native-{VERSION}-cp312-abi3-win_amd64.whl",
        "serverfs-windows-native",
        "serverfs_windows_native",
    ),
    "bridge": (
        f"serverfs_agent_bridge-{VERSION}-py3-none-win_amd64.whl",
        "serverfs-agent-bridge",
        "serverfs_agent_bridge",
    ),
}
COMPONENTS = ("core", "bridge", "cloudflare", "openai")


class InstallRefusal(RuntimeError):
    """Fixed, redacted operator failure."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise InstallRefusal(reason)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance", required=True)
    p.add_argument("--component", required=True, choices=COMPONENTS)
    p.add_argument("--wheel-dir", required=True, type=Path)
    p.add_argument("--core-sha256", required=True)
    p.add_argument("--native-sha256", required=True)
    p.add_argument("--bridge-sha256")
    return p


def plan(component: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    require(component in COMPONENTS, "unknown named component")
    result = ((component, ("core", "native")),)
    return result + (("agent-bridge", ("bridge",)),) if component == "bridge" else result


def pinned_payload(path: Path, kind: str, expected: str) -> bytes:
    filename, dist, package = WHEELS[kind]
    require(
        path.name == filename and re.fullmatch(r"[0-9a-f]{64}", expected) is not None,
        "invalid Wheel filename or SHA256",
    )
    require(
        path.is_file() and not path.is_symlink() and path.stat().st_size <= 64 * 1024 * 1024,
        "invalid Wheel input",
    )
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == expected, "Wheel SHA256 mismatch")
    import io

    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        entries = wheel.infolist()
        names = [item.filename for item in entries]
        require(
            all(item.orig_filename == item.filename for item in entries),
            "Wheel member path is invalid",
        )
        require(
            len(set(names)) == len(names) and wheel.testzip() is None,
            "Wheel archive is corrupt or ambiguous",
        )
        require(
            all(
                not PurePosixPath(n).is_absolute()
                and ".." not in PurePosixPath(n).parts
                and "\\" not in n
                and ":" not in n
                for n in names
            ),
            "Wheel member path is invalid",
        )
        prefix = dist.replace("-", "_") + "-" + VERSION + ".dist-info/"
        records = [n for n in names if n.endswith(".dist-info/METADATA")]
        require(records == [prefix + "METADATA"], "Wheel metadata identity differs")
        info = BytesParser().parsebytes(wheel.read(records[0]))
        wheel_records = [n for n in names if n.endswith(".dist-info/WHEEL")]
        require(wheel_records == [prefix + "WHEEL"], "Wheel tag metadata is missing or ambiguous")
        wheel_info = BytesParser().parsebytes(wheel.read(wheel_records[0]))
        expected_tag = "cp312-abi3-win_amd64" if kind == "native" else "py3-none-win_amd64"
        require(wheel_info.get_all("Tag") == [expected_tag], "Wheel platform tag differs")
        require(
            info["Name"] == dist
            and info["Version"] == VERSION
            and any(n.startswith(package + "/") and not n.endswith("/") for n in names),
            "Wheel package identity differs",
        )
    return data


def run(argv: list[str], env: dict[str, str], cwd: Path, timeout: int) -> bytes:
    result = subprocess.run(
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0, "offline runtime subprocess failed")
    return result.stdout


def offline_env(environ) -> dict[str, str]:
    """Preserve the operator's local cache, not provider/network configuration."""
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "PROGRAMDATA",
        "UV_CACHE_DIR",
    }
    env = {k.upper(): v for k, v in environ.items() if k.upper() in allowed}
    env.update(UV_OFFLINE="1", UV_LINK_MODE="copy", UV_NO_PROGRESS="1")
    return env


def runtime_constraints(uv: str, project: Path, distribution: str, env: dict[str, str]) -> bytes:
    """Export the exact dependency closure from the trusted matching checkout."""
    try:
        config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
        lock = tomllib.loads((project / "uv.lock").read_text(encoding="utf-8"))
        require(
            config["project"]["name"] == distribution
            and config["project"]["version"] == VERSION
            and any(
                item.get("name") == distribution
                and item.get("version") == VERSION
                and item.get("source") == {"editable": "."}
                for item in lock["package"]
            ),
            "runtime lock identity differs",
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InstallRefusal("matching runtime lock is unavailable") from exc
    exported = run(
        [
            uv,
            "--no-config",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements-txt",
        ],
        env,
        project,
        120,
    )
    try:
        lines = [
            line.strip()
            for line in exported.decode("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except UnicodeError as exc:
        raise InstallRefusal("runtime lock export is invalid") from exc
    require(
        bool(lines)
        and all(
            re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9.!+_-]+(?:\s*;[^\r\n]+)?", line)
            for line in lines
        ),
        "runtime lock export contains a non-pinned requirement",
    )
    return exported


def install_locked_runtime(
    uv: str,
    py: Path,
    requirements: Path,
    wheels: list[Path],
    env: dict[str, str],
    cwd: Path,
) -> None:
    """Materialize the frozen closure, then pinned local Wheels, without re-solving it."""
    base = [uv, "--no-config", "pip", "install", "--offline", "--python", str(py)]
    run(
        [*base, "--no-deps", "--only-binary", ":all:", "-r", str(requirements)],
        env,
        cwd,
        240,
    )
    run([*base, "--no-deps", *map(str, wheels)], env, cwd, 240)


def locked_inventory(
    requirements: bytes, kinds: tuple[str, ...], installed: list[list[str]]
) -> int:
    """Check installed versions against the export; uv evaluates PEP 508 markers."""

    def canonical(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    allowed: dict[str, set[str]] = {}
    required: dict[str, str] = {}
    for line in requirements.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement, _, marker = line.partition(";")
        name, version = requirement.strip().split("==", 1)
        key = canonical(name)
        allowed.setdefault(key, set()).add(version)
        if not marker:
            require(
                key not in required or required[key] == version,
                "conflicting unconditional locked versions",
            )
            required[key] = version
    require(bool(allowed), "empty locked dependency inventory")
    for kind in kinds:
        key = canonical(WHEELS[kind][1])
        require(key not in allowed, "project Wheel overlaps dependency export")
        allowed[key] = {VERSION}
        required[key] = VERSION
    seen: dict[str, str] = {}
    for row in installed:
        require(
            isinstance(row, list) and len(row) == 2 and all(isinstance(x, str) for x in row),
            "invalid installed distribution inventory",
        )
        name, version = row
        key = canonical(name)
        require(key not in seen and version in allowed.get(key, ()), "unlocked distribution")
        seen[key] = version
    require(
        all(seen.get(name) == version for name, version in required.items()),
        "required locked distribution missing or differs",
    )
    return len(seen) - len(kinds)


def verify_locked_inventory(
    venv: Path, requirements: bytes, kinds: tuple[str, ...], env: dict[str, str]
) -> int:
    probe = (
        "import importlib.metadata as m,json,sys;from pathlib import Path;"
        "assert Path(sys.prefix).resolve()==Path(sys.argv[1]).resolve();"
        "print(json.dumps([[d.metadata['Name'],d.version] for d in m.distributions()]))"
    )
    raw = run(
        [str(venv / "Scripts/python.exe"), "-I", "-B", "-c", probe, str(venv)],
        env,
        venv,
        45,
    )
    try:
        installed = json.loads(raw)
        require(isinstance(installed, list), "invalid installed distribution inventory")
        return locked_inventory(requirements, kinds, installed)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise InstallRefusal("invalid installed distribution inventory") from exc


def ensure_empty(path: Path) -> None:
    require(
        path.is_dir() and not path.is_symlink() and not any(path.iterdir()),
        "runtime environment must be fresh and empty",
    )


def private_tree(root: Path, sid: str) -> None:
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    storage.check_private(root, directory=True, sid=sid)
    for path in sorted(root.rglob("*"), key=lambda item: (len(item.parts), str(item).casefold())):
        _no_reparse_components(path)
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        require(
            not stat.S_ISLNK(info.st_mode)
            and not info.st_file_attributes & 0x400
            and (directory or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)),
            "installed runtime member is redirected or multiply linked",
        )
        protect_new_file(path, sid)
        storage.check_private(path, directory=directory, sid=sid)


def verify_installed(
    venv: Path, kinds: tuple[str, ...], wheels: dict[str, Path], env: dict[str, str], sid: str
) -> None:
    from serverfs_mcp.windows_v25 import storage

    site = venv / "Lib/site-packages"
    py = venv / "Scripts/python.exe"
    storage.check_private(py, directory=False, sid=sid)
    storage.check_private(venv / "Scripts/pythonw.exe", directory=False, sid=sid)
    for kind in kinds:
        _, dist, package = WHEELS[kind]
        with zipfile.ZipFile(wheels[kind]) as wheel:
            for name in wheel.namelist():
                if name.startswith(package + "/") and not name.endswith("/"):
                    target = site / name
                    storage.check_private(target, directory=False, sid=sid)
                    require(
                        target.read_bytes() == wheel.read(name),
                        "installed module differs from pinned Wheel",
                    )
        probe = (
            "import importlib,importlib.metadata as m,sys;from pathlib import Path;"
            "p=Path(importlib.import_module(sys.argv[1]).__file__).resolve();"
            "assert Path(sys.prefix).resolve()==Path(sys.argv[3]).resolve();"
            "assert p.is_relative_to(Path(sys.prefix).resolve()/'Lib/site-packages');"
            "assert m.version(sys.argv[2])==sys.argv[4];print('VERIFIED')"
        )
        require(
            run(
                [str(py), "-I", "-B", "-c", probe, package, dist, str(venv), VERSION], env, venv, 45
            ).strip()
            == b"VERIFIED",
            "installed distribution import identity differs",
        )


def provision(args: argparse.Namespace) -> dict:
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode,
        "ordinary Windows -I -B controller required",
    )
    from ctypes import windll

    require(not windll.shell32.IsUserAnAdmin(), "do not run as administrator")
    from serverfs_mcp.instance_identity import DEFAULT_INSTANCE_ID, valid_id
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    require(valid_id(args.instance) != DEFAULT_INSTANCE_ID, "named instance required")
    require(
        metadata.version("serverfs-mcp") == metadata.version("serverfs-windows-native") == VERSION,
        "controller distribution version differs",
    )
    controller = Path(sys.executable).resolve(strict=True)
    require(
        sys.prefix != sys.base_prefix and controller.is_relative_to(Path(sys.prefix).resolve()),
        "installed controller virtual environment required",
    )
    import serverfs_mcp

    require(
        Path(serverfs_mcp.__file__)
        .resolve()
        .is_relative_to(Path(sys.prefix).resolve() / "Lib/site-packages"),
        "noninstalled controller source",
    )
    jobs = plan(args.component)
    pins = {"core": args.core_sha256, "native": args.native_sha256}
    if args.component == "bridge":
        require(args.bridge_sha256 is not None, "Bridge Wheel SHA256 required")
        pins["bridge"] = args.bridge_sha256
    else:
        require(args.bridge_sha256 is None, "Bridge Wheel is not required for this component")
    require(args.wheel_dir.is_absolute(), "absolute Wheel directory required")
    _no_reparse_components(args.wheel_dir)
    require(args.wheel_dir.is_dir(), "Wheel directory missing")
    payloads = {}
    for kind, pin in pins.items():
        source = args.wheel_dir / WHEELS[kind][0]
        _no_reparse_components(source)
        payloads[kind] = pinned_payload(source, kind, pin)
    uv = shutil.which("uv")
    require(uv is not None, "trusted uv executable required on PATH")
    uv = str(Path(uv).resolve(strict=True))
    base_python = Path(sys._base_executable).resolve(strict=True)
    _no_reparse_components(base_python)
    require(base_python.is_file(), "controller base Python missing")
    env = offline_env(os.environ)
    if "UV_CACHE_DIR" in env:
        cache = Path(env["UV_CACHE_DIR"])
        require(cache.is_absolute() and cache.is_dir(), "trusted UV_CACHE_DIR must exist")
        cache = cache.resolve(strict=True)
        _no_reparse_components(cache)
        env["UV_CACHE_DIR"] = str(cache)
    # The installer is distributed with a matching trusted source checkout.
    checkout = Path(__file__).resolve().parents[2]
    constraints = {
        "core": runtime_constraints(uv, checkout, WHEELS["core"][1], env),
    }
    if args.component == "bridge":
        constraints["bridge"] = runtime_constraints(
            uv, checkout / "agent_bridge", WHEELS["bridge"][1], env
        )
    root = storage.root(args.instance)
    envs = {role: root / "envs" / role for role, _ in jobs}
    # Validate all existing targets before even preparing missing directories.
    sid = storage._sid()
    for path in envs.values():
        if path.exists() or path.is_symlink():
            storage.check_private(path, directory=True, sid=sid)
            ensure_empty(path)
    storage.prepare(args.component, args.instance)
    home = storage.home(args.component, args.instance)
    storage.check_private(home, directory=True, sid=sid)
    require(
        not any(
            m.instance_id == args.instance for m in storage.component_inventory(args.component)
        ),
        "component manifest already exists",
    )
    for path in envs.values():
        storage.check_private(path, directory=True, sid=sid)
        ensure_empty(path)
    # Exclusive one-shot guard per component (Connector IDs share this venv).
    # Never delete another attempt's directory; failed attempts stay fenced.
    attempt = home / "runtime-install-attempt"
    _no_reparse_components(home)
    attempt.mkdir()
    protect_new_file(attempt, sid)
    storage.check_private(attempt, directory=True, sid=sid)
    report = {
        "state": "INCOMPLETE",
        "verdict": "PENDING",
        "component": args.component,
        "instance": args.instance,
        "wheel_sha256": pins,
        "constraints_sha256": {
            kind: hashlib.sha256(data).hexdigest() for kind, data in constraints.items()
        },
        "installed": [],
        "locked_dependencies_verified": {},
        "phase": "private-input-staging",
    }

    def audit():
        # Only fixed metadata, never a raw subprocess message or credential.
        with (attempt / "audit.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        protect_new_file(attempt / "audit.json", sid)
        storage.check_private(attempt / "audit.json", directory=False, sid=sid)

    try:
        require(
            not any(
                m.instance_id == args.instance for m in storage.component_inventory(args.component)
            ),
            "component manifest appeared",
        )
        # The bytes are pinned before any install and copied into owned storage.
        staged = {}
        for kind, data in payloads.items():
            target = attempt / WHEELS[kind][0]
            with target.open("xb") as stream:
                stream.write(data)
            protect_new_file(target, sid)
            storage.check_private(target, directory=False, sid=sid)
            require(
                hashlib.sha256(target.read_bytes()).hexdigest() == pins[kind],
                "staged Wheel hash differs",
            )
            staged[kind] = target
        staged_constraints = {}
        for kind, data in constraints.items():
            target = attempt / f"{kind}-constraints.txt"
            with target.open("xb") as stream:
                stream.write(data)
            protect_new_file(target, sid)
            storage.check_private(target, directory=False, sid=sid)
            require(
                hashlib.sha256(target.read_bytes()).hexdigest()
                == report["constraints_sha256"][kind],
                "staged runtime constraints differ",
            )
            staged_constraints[kind] = target
        for role, kinds in jobs:
            venv = envs[role]
            report["phase"] = role + "-create-venv"
            ensure_empty(venv)
            run(
                [uv, "--no-config", "venv", "--offline", "--python", str(base_python), str(venv)],
                env,
                attempt,
                120,
            )
            py = venv / "Scripts/python.exe"
            require(py.is_file(), "new runtime interpreter missing")
            report["phase"] = role + "-offline-install"
            lock_kind = "bridge" if kinds == ("bridge",) else "core"
            install_locked_runtime(
                uv, py, staged_constraints[lock_kind], [staged[k] for k in kinds], env, attempt
            )
            report["phase"] = role + "-dependency-check"
            run(
                [uv, "--no-config", "pip", "check", "--python", str(py)],
                env,
                attempt,
                45,
            )
            report["phase"] = role + "-private-tree"
            private_tree(venv, sid)
            report["phase"] = role + "-verify-installed"
            report["locked_dependencies_verified"][role] = verify_locked_inventory(
                venv, constraints[lock_kind], kinds, env
            )
            verify_installed(venv, kinds, staged, env, sid)
            report["installed"].append(role)
        report["phase"] = "verified"
        report["verdict"] = "PASS"
        return report
    except BaseException as exc:
        report["verdict"] = "FAILED"
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        report["state"] = "COMPLETE"
        audit()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = provision(args)
    except Exception as exc:
        # Never show subprocess output, raw exception strings or private paths.
        message = str(exc) if isinstance(exc, InstallRefusal) else type(exc).__name__
        print("runtime install refused: " + message, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

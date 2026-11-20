"""Resume ONLY the known Windows I1 offline-dependency stage after verified uv venv.

This script does not run uv venv/build, delete files, alter existing production
services, or reuse production credentials. It tests the offline resolver before
installing anything and only registers the new i1-smoke Core as Disabled.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

INSTANCE = "i1-smoke"
PORT = 18868
HOST = "i1-smoke.local"
STAGE = "initial"
REPO = Path(__file__).resolve().parents[2]
_WHEEL = "serverfs_mcp-0.24.2-py3-none-any.whl"
_NATIVE = "serverfs_windows_native-0.24.2-cp312-abi3-win_amd64.whl"
_NATIVE_SHA = "127a669511bc72e9d19fd9405a2dbeb32d9cfaaecbb791ea3a5060b6c0ab1df3"


class ResumeRefusal(RuntimeError):
    """A redacted state-specific native I1 recovery refusal."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ResumeRefusal(reason)


def _load_local_script(name: str):
    target = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location("_serverfs_i1_resume_" + name, target)
    require(spec is not None and spec.loader is not None, "trusted helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(argv: list[str], *, env: dict[str, str] | None = None, timeout: int = 120):
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=env,
    )


def _uv_failure(result: subprocess.CompletedProcess[bytes]) -> str:
    # Do not emit full stderr: it may include local user paths or index credentials.
    lowered = result.stderr.decode("utf-8", errors="replace").lower()
    if "no solution found" in lowered or "not found in the cache" in lowered:
        return "offline-dependency-resolution"
    if "no such file" in lowered or "not found" in lowered:
        return "local-input-unavailable"
    if "access is denied" in lowered or "permission denied" in lowered:
        return "private-directory-permission"
    if "incompatible" in lowered or "unsupported" in lowered:
        return "python-or-wheel-compatibility"
    return "unclassified-offline-install-error"


def _trusted_existing_venv(stage, storage, sid: str, root: Path) -> Path:
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

    base = storage.root(INSTANCE)
    require(root == base, "I1 private root does not match the OS account")
    require(
        stage.INSTANCE == INSTANCE and stage.PORT == PORT and stage.HOST == HOST,
        "original fixture source does not match the recovery identity",
    )
    for directory in (
        base,
        base / "envs",
        base / "envs" / "core",
        base / "services",
        base / "services" / "core",
        base / "workdirs",
        base / "workdirs" / "fixture",
        base / "build",
    ):
        storage.check_private(directory, directory=True, sid=sid)
    venv = base / "envs" / "core"
    for file in (
        venv / "pyvenv.cfg",
        venv / "Scripts" / "python.exe",
        venv / "Scripts" / "pythonw.exe",
    ):
        _no_reparse_components(file)
        info = file.lstat()
        require(
            stat.S_ISREG(info.st_mode)
            and not file.is_symlink()
            and not (info.st_file_attributes & 0x400)
            and info.st_nlink == 1,
            "existing fixture Python file cannot be trusted",
        )
    result = _run(
        [
            str(venv / "Scripts" / "python.exe"),
            "-I",
            "-B",
            "-c",
            (
                "import sys; from pathlib import Path; "
                "assert sys.version_info[:3] == (3, 12, 10); "
                f"assert Path(sys.prefix).resolve() == Path({str(venv)!r}).resolve(); "
                "print('I1_PYTHON_MATCH')"
            ),
        ],
        timeout=15,
    )
    require(
        result.returncode == 0 and result.stdout.strip() == b"I1_PYTHON_MATCH",
        "existing isolated Python identity/version is inconsistent",
    )
    site = venv / "Lib" / "site-packages"
    require(site.is_dir(), "isolated site-packages is missing")
    for name in ("serverfs_mcp", "serverfs_windows_native", "mcp", "pydantic"):
        require(not (site / name).exists(), "previous package installation is partial")
    for path in site.iterdir():
        if path.name.lower().startswith(
            ("serverfs_mcp-", "serverfs_windows_native-", "mcp-", "pydantic-")
        ):
            raise ResumeRefusal("previous distribution metadata installation is partial")
    return venv


def _verified_wheels(stage, root: Path) -> tuple[Path, Path]:
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components
    from serverfs_mcp.windows_v25 import storage

    built = root / "build"
    files = tuple(built.glob("*.whl"))
    require(
        len(files) == 1 and files[0].name == _WHEEL,
        "recovery expects exactly the original isolated Product Wheel",
    )
    product = files[0]
    native = REPO / "native" / "windows" / "target" / "wheels" / _NATIVE
    require(
        native.is_file() and stage.sha256(native) == _NATIVE_SHA,
        "pinned native Windows Wheel is absent or has changed",
    )
    _no_reparse_components(product)
    _no_reparse_components(native)
    require(product.is_file() and not product.is_symlink(), "built wheel is not a regular file")
    with zipfile.ZipFile(product) as archive:
        require(archive.testzip() is None, "existing Product Wheel is corrupt")
        for module in (
            "windows_v25/storage.py",
            "windows_v25/runtime.py",
            "windows_v25/cli.py",
            "windows_v25/isolation.py",
            "windows_v25/spec.py",
            "instance_identity.py",
        ):
            data = archive.read("serverfs_mcp/" + module)
            require(
                data == (REPO / "src" / "serverfs_mcp" / module).read_bytes(),
                "isolated Wheel no longer matches the source checkpoint",
            )
    storage.check_private(built, directory=True)
    return product, native


def _check_owner_tasks() -> list[dict]:
    from serverfs_mcp.windows_v25 import storage

    smoke = _load_local_script("v0250-i1-live-smoke.py")
    previous = smoke.production_snapshot()
    command = (
        "$ErrorActionPreference='Stop';"
        "$tasks=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {"
        "$_.TaskPath -eq '\\' -and $_.TaskName -ceq 'ServerFS v25 i1-smoke Core'});"
        "[Console]::Out.Write($tasks.Count)"
    )
    result = _run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        timeout=20,
    )
    require(
        result.returncode == 0 and result.stdout.strip() == b"0",
        "isolated Core task may already be registered",
    )
    # Keep an explicit exact scoped identity for later stage verification.
    require(storage.root(INSTANCE).is_dir(), "I1 instance directory is absent")
    return previous


def _offline_install(python: Path, product: Path, native: Path) -> None:
    global STAGE

    args = [
        "uv",
        "pip",
        "install",
        "--offline",
        "--python",
        str(python),
        str(product),
        str(native),
    ]
    environment = dict(os.environ)
    environment["UV_LINK_MODE"] = "copy"
    STAGE = "offline-resolution"
    probe = _run([*args, "--dry-run"], env=environment)
    if probe.returncode:
        print("I1_RESUME_OFFLINE_RESOLUTION=FAILED category=" + _uv_failure(probe))
        raise ResumeRefusal("offline dependency resolution is unavailable")
    print("I1_RESUME_OFFLINE_RESOLUTION=PASS")
    STAGE = "offline-install"
    installed = _run(args, env=environment)
    if installed.returncode:
        print("I1_RESUME_OFFLINE_INSTALL=FAILED category=" + _uv_failure(installed))
        raise ResumeRefusal("offline dependency installation failed")
    print("I1_RESUME_OFFLINE_INSTALL=PASS")


def main() -> None:
    global STAGE

    require(sys.platform == "win32" and os.name == "nt", "Windows host required")
    require(sys.flags.isolated and sys.dont_write_bytecode, "Python -I -B required")
    require(not ctypes.windll.shell32.IsUserAnAdmin(), "run non-elevated")
    stage = _load_local_script("v0250-i1-stage.py")
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    sid = current_user_sid()
    root = storage.root(INSTANCE)
    STAGE = "existing-state-validation"
    venv = _trusted_existing_venv(stage, storage, sid, root)
    product, native = _verified_wheels(stage, root)
    for artifact in (
        root / "services" / "core" / "serverfs.toml",
        root / "services" / "core" / "http.env",
        root / "services" / "core" / "service.json",
        root / "services" / "core" / "task.xml",
    ):
        require(
            not artifact.exists() and not artifact.is_symlink(),
            "fixture policy, credential, manifest or task already exists",
        )
    baseline = _check_owner_tasks()
    print("I1_RESUME_EXISTING_STATE=VERIFIED")
    _offline_install(venv / "Scripts" / "python.exe", product, native)

    STAGE = "wheel-bytes-and-dacl"
    stage.protect_tree(venv, sid)
    require(
        stage.sha256(venv / "Lib" / "site-packages" / "serverfs_mcp" / "windows_v25" / "runtime.py")
        == stage.sha256(REPO / "src" / "serverfs_mcp" / "windows_v25" / "runtime.py"),
        "installed package bytes do not match the Wheel source",
    )
    stage.run(
        [
            str(venv / "Scripts" / "python.exe"),
            "-I",
            "-B",
            "-c",
            "import serverfs_mcp.windows_v25.runtime, serverfs_windows_native, mcp",
        ]
    )
    print("I1_RESUME_WHEEL_AND_ACL=PASS")

    STAGE = "new-private-policy-and-key"
    service = root / "services" / "core"
    fixture = root / "workdirs" / "fixture"
    import secrets

    policy = (
        "[server]\nlog_level = 'info'\n\n[[workdirs]]\n"
        "alias = 'Fixture'\n"
        "path = " + json.dumps(str(fixture), ensure_ascii=False) + "\n"
        "read_only = false\nagent_mode = 'disabled'\nrepo_ops_enabled = false\n"
    )
    stage.private_file(service / "serverfs.toml", policy.encode("utf-8"), sid)
    auth = (
        "SERVERFS_HTTP_API_KEY=" + secrets.token_urlsafe(48) + "\n"
        "SERVERFS_HTTP_ALLOWED_HOSTS=" + HOST + f",127.0.0.1:{PORT}\n"
        "SERVERFS_OAUTH_ENABLED=false\n"
    )
    stage.private_file(service / "http.env", auth.encode("utf-8"), sid)
    print("I1_RESUME_PRIVATE_POLICY_AND_KEY=PASS")

    STAGE = "configure-private-Core"
    python = str(venv / "Scripts" / "python.exe")
    result = stage.run(
        [
            python,
            "-I",
            "-B",
            "-m",
            "serverfs_mcp.windows_v25.cli",
            "core",
            "configure",
            "--instance",
            INSTANCE,
            "--config",
            str(service / "serverfs.toml"),
            "--auth-file",
            str(service / "http.env"),
            "--port",
            str(PORT),
            "--hostname",
            HOST,
        ]
    )
    require(json.loads(result).get("configured") is True, "isolated Core configure was refused")

    STAGE = "register-new-Core-Disabled"
    response = stage.run(
        [
            python,
            "-I",
            "-B",
            "-m",
            "serverfs_mcp.windows_v25.cli",
            "core",
            "install",
            "--instance",
            INSTANCE,
        ]
    )
    task = json.loads(response)
    require(
        task.get("task_name") == "ServerFS v25 i1-smoke Core"
        and task.get("registered") is True
        and task.get("enabled") is False
        and task.get("running") is False,
        "new Core was not registered Disabled",
    )
    print("I1_RESUME_TASK_DISABLED=PASS")

    STAGE = "production-task-postflight"
    smoke = _load_local_script("v0250-i1-live-smoke.py")
    require(
        baseline == smoke.production_snapshot(),
        "production task state changed across the isolated resume",
    )
    print("I1_RESUME_PRODUCTION_TASKS=UNCHANGED")
    print("I1_RESUME=PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("I1_RESUME=FAILED stage=" + STAGE + " type=" + type(exc).__name__, file=sys.stderr)
        print("I1_RESUME_PRODUCTION_TASKS=NOT_TARGETED", file=sys.stderr)
        raise SystemExit(2) from None

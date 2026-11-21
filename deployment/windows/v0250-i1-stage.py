"""One-use Windows I1 file-only Core staging (NO production mutation).

Run under the repository's .venv, native normal-user Python -I -B.
Registers only ServerFS v25 i1-smoke Core, Disabled. NEVER starts it.
This deliberately fails closed instead of adopting or fixing existing paths.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

INSTANCE = "i1-smoke"
HOST = "i1-smoke.local"
PORT = 18868
NATIVE_WHEEL_SHA256 = "127a669511bc72e9d19fd9405a2dbeb32d9cfaaecbb791ea3a5060b6c0ab1df3"
REPO = Path(__file__).resolve().parents[2]
STAGE = "initial"


class StageRefusal(RuntimeError):
    """Fail without exposing credentials, private paths, or provider output."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise StageRefusal(message)


def run(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=150,
        check=False,
    )
    if result.returncode:
        raise StageRefusal("isolated step failed; consult the stage marker")
    return result.stdout.decode("utf-8", errors="replace").strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def protect_tree(root: Path, sid: str) -> None:
    """Protect exclusively this freshly created venv; never mutate an older path."""
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    storage.check_private(root, directory=True, sid=sid)
    paths = sorted(root.rglob("*"), key=lambda path: (len(path.parts), str(path).casefold()))
    for path in paths:
        _no_reparse_components(path)
        info = path.lstat()
        is_dir = stat.S_ISDIR(info.st_mode)
        require(
            not path.is_symlink()
            and not (info.st_file_attributes & 0x400)
            and (is_dir or stat.S_ISREG(info.st_mode)),
            "installed package path is redirected or not regular",
        )
        if not is_dir:
            require(info.st_nlink == 1, "installed package file has another hardlink")
        protect_new_file(path, sid)
        storage.check_private(path, directory=is_dir, sid=sid)


def private_file(path: Path, data: bytes, sid: str) -> None:
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    require(not path.exists() and not path.is_symlink(), "isolated artifact is already occupied")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    protect_new_file(path, sid)
    storage.check_private(path, directory=False, sid=sid)


def preflight() -> None:
    require(sys.platform == "win32" and os.name == "nt", "native Windows is required")
    require(bool(sys.flags.isolated and sys.dont_write_bytecode), "Python -I -B is required")
    require(not ctypes.windll.shell32.IsUserAnAdmin(), "non-elevated Windows user is required")
    gate = REPO / "deployment" / "windows" / "v0250-i1-native-preflight.ps1"
    output = run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(gate),
            "-InstanceId",
            INSTANCE,
            "-Port",
            str(PORT),
        ]
    )
    require("I1_NATIVE_PREFLIGHT=PASS" in output, "host preflight was not accepted")


def main() -> None:
    global STAGE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    STAGE = "preflight"
    preflight()
    sid = current_user_sid()
    home = storage.root(INSTANCE)
    venv = home / "envs" / "core"
    python = venv / "Scripts" / "python.exe"
    pythonw = venv / "Scripts" / "pythonw.exe"
    service = storage.home("core", INSTANCE)
    fixture = home / "workdirs" / "fixture"
    config = service / "serverfs.toml"
    auth = service / "http.env"
    native = (
        REPO
        / "native"
        / "windows"
        / "target"
        / "wheels"
        / "serverfs_windows_native-0.24.2-cp312-abi3-win_amd64.whl"
    )
    require(
        native.is_file() and sha256(native) == NATIVE_WHEEL_SHA256,
        "original pinned native Windows Wheel differs",
    )

    STAGE = "private-directories"
    storage.prepare("core", INSTANCE)
    for directory in (home / "workdirs", fixture, home / "build"):
        require(
            not directory.exists() and not directory.is_symlink(),
            "new instance directory is already occupied",
        )
        directory.mkdir()
        protect_new_file(directory, sid)
        storage.check_private(directory, directory=True, sid=sid)
    print("I1_PRIVATE_INSTANCE_DIRECTORIES=PASS")

    STAGE = "offline-product-wheel"
    build = home / "build"
    run(
        [
            "uv",
            "build",
            "--wheel",
            "--offline",
            "--out-dir",
            str(build),
        ],
        cwd=REPO,
    )
    wheels = tuple(build.glob("serverfs_mcp-*.whl"))
    require(len(wheels) == 1, "local source must produce exactly one Product Wheel")
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        require(archive.testzip() is None, "built wheel is corrupt")
        for module in (
            "windows_v25/storage.py",
            "windows_v25/runtime.py",
            "windows_v25/cli.py",
            "windows_v25/isolation.py",
            "windows_v25/spec.py",
            "instance_identity.py",
        ):
            actual = archive.read("serverfs_mcp/" + module)
            original = (REPO / "src" / "serverfs_mcp" / module).read_bytes()
            require(actual == original, "new wheel does not match current source")
    product_hash = sha256(wheel)
    print("I1_SOURCE_WHEEL=VERIFIED")

    STAGE = "offline-venv"
    require(
        venv.is_dir() and not any(venv.iterdir()), "isolated venv directory must be new and empty"
    )
    run(["uv", "venv", "--offline", "--python", "3.12.10", str(venv)])
    require(python.is_file() and pythonw.is_file(), "new Python executables absent")
    private_env = dict(os.environ)
    private_env["UV_LINK_MODE"] = "copy"
    install_args = [
        "uv",
        "pip",
        "install",
        "--offline",
        "--python",
        str(python),
        str(wheel),
        str(native),
    ]
    # --no-index suppresses registry metadata, including offline cached index
    # metadata. Resolve first with offline cache only, without writing a venv.
    STAGE = "offline-dependency-resolution"
    run([*install_args, "--dry-run"], env=private_env)
    STAGE = "offline-dependency-install"
    run(install_args, env=private_env)
    print("I1_OFFLINE_ENVIRONMENT=PASS")

    STAGE = "wheel-bytes-and-dacl"
    protect_tree(venv, sid)
    require(
        sha256(venv / "Lib" / "site-packages" / "serverfs_mcp" / "windows_v25" / "runtime.py")
        == sha256(REPO / "src" / "serverfs_mcp" / "windows_v25" / "runtime.py"),
        "installed v25 code bytes differ from source",
    )
    run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            "import serverfs_mcp.windows_v25.runtime, serverfs_windows_native, mcp",
        ]
    )
    print("I1_INSTALLED_WHEEL_AND_DACL=PASS")

    STAGE = "private-policy-and-new-auth"
    doc = (
        "[server]\nlog_level = 'info'\n\n[[workdirs]]\n"
        "alias = 'Fixture'\n"
        "path = " + json.dumps(str(fixture), ensure_ascii=False) + "\n"
        "read_only = false\nagent_mode = 'disabled'\nrepo_ops_enabled = false\n"
    )
    private_file(config, doc.encode("utf-8"), sid)
    key = secrets.token_urlsafe(48)
    payload = (
        "SERVERFS_HTTP_API_KEY=" + key + "\n"
        "SERVERFS_HTTP_ALLOWED_HOSTS=" + HOST + ",127.0.0.1:" + str(PORT) + "\n"
        "SERVERFS_OAUTH_ENABLED=false\n"
    )
    private_file(auth, payload.encode("utf-8"), sid)
    print("I1_ISOLATED_POLICY_AND_KEY=PASS")

    STAGE = "configure-private-manifest"
    out = run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "serverfs_mcp.windows_v25.cli",
            "core",
            "configure",
            "--instance",
            INSTANCE,
            "--config",
            str(config),
            "--auth-file",
            str(auth),
            "--port",
            str(PORT),
            "--hostname",
            HOST,
        ]
    )
    require(json.loads(out).get("configured") is True, "private Core was not configured")

    STAGE = "register-disabled-only"
    out = run(
        [
            str(python),
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
    task = json.loads(out)
    require(
        task.get("registered") is True
        and task.get("enabled") is False
        and task.get("running") is False
        and task.get("task_name") == "ServerFS v25 i1-smoke Core",
        "new Core was not registered Disabled",
    )
    print("I1_TASK_REGISTERED_DISABLED=PASS")
    print("I1_PRODUCT_WHEEL_SHA256=" + product_hash)
    print("I1_STAGE=PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("I1_STAGE=FAILED stage=" + STAGE + " type=" + type(exc).__name__, file=sys.stderr)
        print("I1_PRODUCTION_SERVICES=NOT_TARGETED", file=sys.stderr)
        raise SystemExit(2) from None

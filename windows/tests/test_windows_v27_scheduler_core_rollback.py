"""Historical installed file-only Core rollback through the real Windows Scheduler.

Opt-in on disposable GitHub-hosted Windows runners only. No provider, Tunnel,
existing ServerFS instance, or production task is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_cloudflare_security import _no_reparse_components
from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
from serverfs_mcp.windows_private_artifacts import protect_new_file
from serverfs_mcp.windows_v25 import storage
from serverfs_mcp.windows_v25.readiness import _current_identity
from serverfs_mcp.windows_v25.spec import Manifest
from serverfs_mcp.windows_v25.tasks import Scheduler

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("SERVERFS_WINDOWS_SCHEDULER_ROLLBACK") != "1",
    reason="explicit disposable Windows Scheduler rollback opt-in required",
)

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
BASELINE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"


def _run(*argv: str, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        list(argv),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return result.stdout.strip()


def _one(directory: Path, pattern: str) -> Path:
    files = list(directory.glob(pattern))
    assert len(files) == 1
    with zipfile.ZipFile(files[0]) as wheel:
        assert wheel.testzip() is None
    return files[0]


def _build(source: Path, output: Path, uv: str, env: dict[str, str]) -> tuple[Path, Path, Path]:
    output.mkdir()
    req = output / "requirements.txt"
    _run(uv, "build", "--wheel", "--out-dir", str(output), cwd=source, env=env)
    product = _one(output, "serverfs_mcp-*.whl")
    _run(
        uv,
        "export",
        "--locked",
        "--no-dev",
        "--no-emit-project",
        "--output-file",
        str(req),
        cwd=source,
        env=env,
    )
    native = source / "native/windows"
    _run(
        uv,
        "tool",
        "run",
        "--from",
        "maturin==1.8.2",
        "maturin",
        "build",
        "--release",
        "--features",
        "pyo3",
        "--locked",
        cwd=native,
        env=env,
    )
    kernel = _one(native / "target/wheels", "serverfs_windows_native-*.whl")
    return product, kernel, req


def _protect_venv(venv: Path, sid: str) -> None:
    # The entire venv is test-owned. No cache hardlinks or reparse targets may be ACL-ed.
    storage.check_private(venv, directory=True, sid=sid)
    for path in venv.rglob("*"):
        _no_reparse_components(path)
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        assert not path.is_symlink() and not info.st_file_attributes & 0x400
        assert directory or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        protect_new_file(path, sid)
        storage.check_private(path, directory=directory, sid=sid)


def _install(
    release: tuple[Path, Path, Path], python: Path, sid: str, uv: str, env: dict[str, str]
) -> None:
    product, kernel, req = release
    _run(uv, "pip", "sync", "--python", str(python), str(req), cwd=ROOT, env=env)
    _run(
        uv,
        "pip",
        "install",
        "--no-deps",
        "--reinstall",
        "--python",
        str(python),
        str(product),
        str(kernel),
        cwd=ROOT,
        env=env,
    )
    venv = python.parent.parent
    _protect_venv(venv, sid)
    packages = venv / "Lib/site-packages"
    for path, prefix in ((product, "serverfs_mcp/"), (kernel, "serverfs_windows_native/")):
        with zipfile.ZipFile(path) as wheel:
            members = [n for n in wheel.namelist() if n.startswith(prefix) and not n.endswith("/")]
            assert members
            installed = {
                item.relative_to(packages).as_posix()
                for item in (packages / prefix).rglob("*")
                if item.is_file() and "__pycache__" not in item.parts
            }
            assert installed == set(members), "installed package contains stale or missing files"
            for name in members:
                assert (packages / name).read_bytes() == wheel.read(name)
        print(f"INSTALLED_WHEEL_SHA256={hashlib.sha256(path.read_bytes()).hexdigest()}")
    _run(
        str(python),
        "-I",
        "-B",
        "-c",
        "import pathlib,sys,serverfs_mcp,serverfs_windows_native;"
        "assert all(pathlib.Path(m.__file__).resolve().is_relative_to("
        "pathlib.Path(sys.prefix).resolve()) "
        "for m in (serverfs_mcp,serverfs_windows_native))",
        cwd=venv,
        env=env,
    )


def _private_file(path: Path, content: str, sid: str) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(content)
    protect_new_file(path, sid)
    storage.check_private(path, directory=False, sid=sid)


async def _mcp_read(port: int, token: str) -> None:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=10, trust_env=False
    ) as http:
        transport = streamable_http_client(
            f"http://127.0.0.1:{port}/mcp", http_client=http, terminate_on_close=False
        )
        async with Client(transport, mode="2026-07-28", raise_exceptions=True) as client:
            listed = await client.call_tool("list_workdirs", {})
            assert not listed.is_error
            assert {item["alias"] for item in listed.structured_content["workdirs"]} == {"Fixture"}
            read = await client.call_tool(
                "read_text_file", {"workdir": "Fixture", "path": "marker.txt"}
            )
            assert not read.is_error and read.structured_content["content"] == "rollback-proof"


def test_historical_file_only_core_scheduler_upgrade_and_rollback(tmp_path: Path) -> None:
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    assert not storage.root().exists() and not storage.root().is_symlink()
    assert not tmp_path.resolve().is_relative_to(REPO)
    uv = shutil.which("uv")
    assert uv is not None
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON", "UV_PROJECT"))
        and key.upper() not in {"VIRTUAL_ENV", "CONDA_PREFIX", "UV_OFFLINE", "CARGO_NET_OFFLINE"}
    }
    env.update(UV_LINK_MODE="copy", UV_PYTHON=str(Path(sys._base_executable).resolve()))
    assert _run("git", "rev-parse", "HEAD", cwd=REPO, env=env) == os.environ["GITHUB_SHA"]
    old = tmp_path / "historical"
    old.mkdir()
    archive = tmp_path / "historical.tar"
    _run("git", "archive", "--output", str(archive), BASELINE, cwd=REPO, env=env)
    with tarfile.open(archive) as bundle:
        bundle.extractall(old, filter="data")
    baseline = _build(old, tmp_path / "old-wheels", uv, env)
    candidate = _build(ROOT, tmp_path / "new-wheels", uv, env)
    # Recheck that no existing ServerFS instance appeared during the builds.
    assert not storage.root().exists() and not storage.root().is_symlink()
    sid = current_user_sid()
    instance = "w27gate" + uuid4().hex[:16]
    root = storage.root(instance)
    assert not root.exists() and not root.is_symlink()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "marker.txt").write_text("rollback-proof", encoding="utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    hostname = "core.rollback.fixture"
    token = secrets.token_hex(32)
    config = root / "core.toml"
    auth = root / "auth.env"
    pythonw = storage.pinned_pythonw("core", instance)
    python = pythonw.with_name("python.exe")
    manifest = Manifest.parse(
        {
            "schema": 2,
            "component": "core",
            "instance_id": instance,
            "sid": sid,
            "pythonw": str(pythonw),
            "settings": {
                "config": str(config),
                "auth_file": str(auth),
                "port": port,
                "hostname": hostname,
            },
        }
    )
    scheduler = Scheduler()
    assert scheduler.status(manifest)["registered"] is False
    storage.prepare("core", instance)
    _private_file(
        config,
        "[[workdirs]]\nalias = 'Fixture'\n"
        f"path = {json.dumps(workspace.as_posix())}\nread_only = true\n",
        sid,
    )
    _private_file(
        auth,
        f"SERVERFS_HTTP_API_KEY={token}\nSERVERFS_HTTP_ALLOWED_HOSTS={hostname},127.0.0.1:{port}\n",
        sid,
    )
    _run(
        uv,
        "venv",
        "--python",
        str(Path(sys._base_executable).resolve()),
        str(python.parent.parent),
        cwd=ROOT,
        env=env,
    )
    _install(baseline, python, sid, uv, env)
    storage.stage(manifest)
    home = storage.home("core", instance)
    protected = (config, auth, home / "service.json")
    expected = {p: p.read_bytes() for p in protected}
    generations = set()
    attempted = False
    try:
        attempted = True
        installed = scheduler.install(manifest)
        assert installed["enabled"] is False and installed["running"] is False
        task_xml = home / "task.xml"
        expected[task_xml] = task_xml.read_bytes()
        for index, release in enumerate((baseline, candidate, baseline)):
            assert scheduler.status(manifest)["enabled"] is False
            assert _current_identity(manifest) is None
            if index:
                _install(release, python, sid, uv, env)
            state = scheduler.start(manifest)
            assert state["running"] is True and state["readiness"] == "verified"
            identity = _current_identity(manifest)
            assert identity is not None
            generation = (identity.pid, identity.creation_time)
            assert generation not in generations
            generations.add(generation)
            asyncio.run(_mcp_read(port, token))
            stopped = scheduler.stop(manifest)
            assert stopped["enabled"] is False and stopped["running"] is False
            assert _current_identity(manifest) is None
            assert not any(row.port == port for row in _windows_ipv4_tcp_listeners())
            assert all(p.read_bytes() == expected[p] for p in expected)
            assert storage.load("core", instance) == manifest
        assert len(generations) == 3
    finally:
        # Exact manifest/XML owner checks remain mandatory even after a partial Create.
        # Never kill a PID or delete unknown state. The hosted VM owns fixture files.
        if attempted and scheduler.status(manifest)["registered"]:
            scheduler.stop(manifest)
            assert _current_identity(manifest) is None
            assert not any(row.port == port for row in _windows_ipv4_tcp_listeners())
            assert scheduler.uninstall(manifest)["registered"] is False
        assert scheduler.status(manifest)["registered"] is False
    print("WINDOWS_HISTORICAL_SCHEDULER_CORE_ROLLBACK_PASS")

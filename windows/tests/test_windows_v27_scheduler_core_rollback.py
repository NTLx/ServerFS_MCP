"""Historical installed file-only Core rollback through the real Windows Scheduler.

Opt-in on a disposable Windows runner or an explicitly authorized ordinary-user
host. Only a random, previously absent instance and task may be mutated.
Pre-existing tasks and instance names must remain unchanged.
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
from uuid import UUID, uuid4

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
    reason="explicit isolated Windows Scheduler rollback opt-in required",
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


def _protected_tasks(excluded: str = "") -> dict[str, tuple[str, str, tuple]]:
    """Protect exact definitions and running instances, not periodic trigger timestamps."""
    command = (
        "$ErrorActionPreference='Stop';"
        "$service=New-Object -ComObject Schedule.Service;$service.Connect();"
        "$tasks=@(Get-ScheduledTask | Where-Object {$_.TaskName -like 'ServerFS*'});"
        "$rows=@($tasks|ForEach-Object{"
        "$registered=$service.GetFolder($_.TaskPath).GetTask($_.TaskName);"
        "$xml=$registered.Xml;"
        "$running=$registered.GetInstances(0);"
        "$instances=@(for($i=1;$i -le $running.Count;$i++){"
        "$item=$running.Item($i);"
        "[pscustomobject]@{guid=$item.InstanceGuid;engine=[int]$item.EnginePID}});"
        "$hash=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create()."
        "ComputeHash([Text.Encoding]::UTF8.GetBytes($xml)));"
        "[pscustomobject]@{name=$_.TaskName;path=$_.TaskPath;"
        "state=$_.State.ToString();hash=$hash;instances=$instances}});"
        "ConvertTo-Json -InputObject $rows -Depth 5 -Compress"
    )
    rows = json.loads(
        _run(
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
            cwd=ROOT,
            env=dict(os.environ),
        )
    )
    assert isinstance(rows, list)
    result = {}
    for row in rows:
        assert row["path"] == chr(92)
        name = row["name"].casefold()
        assert name not in result
        if name != excluded.casefold():
            assert "w27gate" not in name
            state = row["state"]
            assert state in {"Running", "Ready", "Disabled"}
            assert isinstance(row["instances"], list)
            assert all(isinstance(item, dict) for item in row["instances"]), (
                "Scheduler instance JSON serialization is incomplete"
            )
            instances = tuple(
                sorted((str(UUID(item["guid"])), item["engine"]) for item in row["instances"])
            )
            assert all(type(engine) is int and engine > 0 for _guid, engine in instances)
            assert len(instances) == (1 if state == "Running" else 0)
            result[name] = (state, row["hash"], instances)
    return result


def _existing_instances() -> set[str]:
    parent = storage.root() / "instances"
    if not parent.exists():
        assert not parent.is_symlink()
        return set()
    storage.check_private(parent, directory=True, sid=current_user_sid())
    return {item.name for item in parent.iterdir()}


def _remove_owned_root(root: Path, instance: str, sid: str) -> None:
    assert root == storage.root(instance) and instance.startswith("w27gate")
    assert root.parent == storage.root() / "instances"
    storage.check_private(root, directory=True, sid=sid)
    for path in root.rglob("*"):
        _no_reparse_components(path)
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        assert directory or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        assert not path.is_symlink() and not info.st_file_attributes & 0x400
    shutil.rmtree(root)
    assert not root.exists()


def test_historical_file_only_core_scheduler_upgrade_and_rollback(tmp_path: Path) -> None:
    local = os.environ.get("SERVERFS_WINDOWS_SCHEDULER_ROLLBACK_LOCAL") == "1"
    if local:
        assert os.environ.get("GITHUB_ACTIONS") != "true"
        assert not current_user_sid().endswith("-500")
        storage.check_private(storage.root(), directory=True)
        storage.check_private(storage.root() / "instances", directory=True)
        assert _protected_tasks(), "local host must have a verified baseline"
    else:
        assert os.environ.get("GITHUB_ACTIONS") == "true"
        assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        assert not storage.root().exists() and not storage.root().is_symlink()
    prior_tasks = _protected_tasks()
    prior_instances = _existing_instances()
    assert not any(name.startswith("w27gate") for name in prior_instances), "prior fixture residue"
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
    if local:
        env.update(UV_OFFLINE="1", CARGO_NET_OFFLINE="true")
        expected_sha = os.environ["SERVERFS_WINDOWS_SCHEDULER_ROLLBACK_SHA"]
    else:
        expected_sha = os.environ["GITHUB_SHA"]
    assert _run("git", "rev-parse", "HEAD", cwd=REPO, env=env) == expected_sha
    old = tmp_path / "historical"
    old.mkdir()
    archive = tmp_path / "historical.tar"
    _run("git", "archive", "--output", str(archive), BASELINE, cwd=REPO, env=env)
    with tarfile.open(archive) as bundle:
        bundle.extractall(old, filter="data")
    candidate_source = tmp_path / "candidate"
    candidate_source.mkdir()
    candidate_archive = tmp_path / "candidate.tar"
    _run(
        "git",
        "archive",
        "--output",
        str(candidate_archive),
        f"{expected_sha}:windows",
        cwd=REPO,
        env=env,
    )
    with tarfile.open(candidate_archive) as bundle:
        bundle.extractall(candidate_source, filter="data")
    baseline = _build(old, tmp_path / "old-wheels", uv, env)
    candidate = _build(candidate_source, tmp_path / "new-wheels", uv, env)
    # No installed task or instance may have changed during the builds.
    assert _protected_tasks() == prior_tasks
    assert _existing_instances() == prior_instances
    if not local:
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
    passed = False
    try:
        attempted = True
        installed = scheduler.install(manifest)
        assert installed["enabled"] is False and installed["running"] is False
        task_xml = home / "task.xml"
        expected[task_xml] = task_xml.read_bytes()
        for index, release in enumerate((baseline, candidate, baseline)):
            assert _protected_tasks(manifest.task_name()) == prior_tasks
            assert _existing_instances() == prior_instances | {instance}
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
            assert _protected_tasks(manifest.task_name()) == prior_tasks
        assert len(generations) == 3
        passed = True
    finally:
        # Exact manifest/XML owner checks remain mandatory even after a partial Create.
        # Never kill a PID or delete unknown state. Preserve failed fixture files.
        if attempted and scheduler.status(manifest)["registered"]:
            scheduler.stop(manifest)
            assert _current_identity(manifest) is None
            assert not any(row.port == port for row in _windows_ipv4_tcp_listeners())
            assert scheduler.uninstall(manifest)["registered"] is False
        assert scheduler.status(manifest)["registered"] is False
        assert _protected_tasks() == prior_tasks
        assert _existing_instances() == prior_instances | {instance}
        if passed and local:
            _remove_owned_root(root, instance, sid)
            assert _existing_instances() == prior_instances
    print("WINDOWS_HISTORICAL_SCHEDULER_CORE_ROLLBACK_PASS")

"""Opt-in actual independent Windows Bridge Scheduler migration and rollback.

Use only a previously absent random instance, exact Git archives, locked
installed owner/worker wheels, a synthetic workspace and signed Named Pipe.
No production task, configuration, credential, Tunnel or provider is changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from serverfs_mcp.agent_client import AgentBridgeRemoteError
from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient
from serverfs_mcp.agent_lifecycle import bridge_child_environment
from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_process import process_identity
from serverfs_mcp.windows_v25 import storage
from serverfs_mcp.windows_v25.bridge_peer_job import make_bridge_peer_validator
from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources
from serverfs_mcp.windows_v25.readiness import _current_identity
from serverfs_mcp.windows_v25.spec import Manifest
from serverfs_mcp.windows_v25.tasks import Scheduler
from test_windows_v25_bridge_v2_real_worker import _PROVISION
from test_windows_v27_scheduler_core_rollback import (
    BASELINE,
    REPO,
    ROOT,
    _build,
    _existing_instances,
    _install,
    _one,
    _private_file,
    _protect_venv,
    _protected_tasks,
    _remove_owned_root,
    _run,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("SERVERFS_WINDOWS_BRIDGE_ROLLBACK") != "1",
    reason="explicit ordinary-user independent Bridge Scheduler rollback required",
)


def _worker_build(source: Path, output: Path, uv: str, env: dict[str, str]):
    output.mkdir()
    req = output / "requirements.txt"
    _run(uv, "build", "--offline", "--wheel", "--out-dir", str(output), cwd=source, env=env)
    wheel = _one(output, "serverfs_agent_bridge-*.whl")
    _run(
        uv,
        "export",
        "--offline",
        "--locked",
        "--no-dev",
        "--no-emit-project",
        "--output-file",
        str(req),
        cwd=source,
        env=env,
    )
    return wheel, req


def _worker_install(release, python: Path, sid: str, uv: str, env: dict[str, str]) -> None:
    wheel, req = release
    _run(uv, "pip", "sync", "--offline", "--python", str(python), str(req), cwd=ROOT, env=env)
    _run(
        uv,
        "pip",
        "install",
        "--offline",
        "--no-deps",
        "--reinstall",
        "--python",
        str(python),
        str(wheel),
        cwd=ROOT,
        env=env,
    )
    _protect_venv(python.parent.parent, sid)
    packages = python.parent.parent / "Lib/site-packages"
    prefix = "serverfs_agent_bridge/"
    with zipfile.ZipFile(wheel) as bundle:
        members = {n for n in bundle.namelist() if n.startswith(prefix) and not n.endswith("/")}
        assert members
        installed = {
            p.relative_to(packages).as_posix()
            for p in (packages / prefix).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        assert installed == members
        for name in members:
            assert (packages / name).read_bytes() == bundle.read(name)
    _run(
        str(python),
        "-I",
        "-B",
        "-c",
        "import pathlib,sys,serverfs_agent_bridge as b;"
        "assert pathlib.Path(b.__file__).resolve().is_relative_to("
        "pathlib.Path(sys.prefix).resolve())",
        cwd=python.parent.parent,
        env=env,
    )
    print(f"INSTALLED_BRIDGE_WHEEL_SHA256={hashlib.sha256(wheel.read_bytes()).hexdigest()}")


def test_historical_independent_bridge_scheduler_rollback(tmp_path: Path) -> None:
    assert os.environ.get("GITHUB_ACTIONS") != "true"
    sid = current_user_sid()
    assert not sid.endswith("-500")
    storage.check_private(storage.root(), directory=True, sid=sid)
    storage.check_private(storage.root() / "instances", directory=True, sid=sid)
    prior_tasks = _protected_tasks()
    assert prior_tasks
    prior_instances = _existing_instances()
    assert not any(name.startswith("w27gate") for name in prior_instances)
    assert not tmp_path.resolve().is_relative_to(REPO)
    uv = shutil.which("uv")
    assert uv
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON", "UV_PROJECT"))
        and k.upper() not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    env.update(
        UV_OFFLINE="1",
        CARGO_NET_OFFLINE="true",
        UV_LINK_MODE="copy",
        UV_PYTHON=str(Path(sys._base_executable).resolve()),
    )
    expected = os.environ["SERVERFS_WINDOWS_BRIDGE_ROLLBACK_SHA"]
    assert _run("git", "rev-parse", "HEAD", cwd=REPO, env=env) == expected
    releases = []
    for name, tree in (("old", BASELINE), ("new", expected + ":windows")):
        source = tmp_path / name
        source.mkdir()
        archive = tmp_path / (name + ".tar")
        _run("git", "archive", "--output", str(archive), tree, cwd=REPO, env=env)
        with tarfile.open(archive) as bundle:
            bundle.extractall(source, filter="data")
        releases.append(
            (
                _build(source, tmp_path / (name + "-owner"), uv, env),
                _worker_build(source / "agent_bridge", tmp_path / (name + "-worker"), uv, env),
            )
        )
    assert _protected_tasks() == prior_tasks and _existing_instances() == prior_instances

    instance = "w27gate" + uuid4().hex[:16]
    root = storage.root(instance)
    assert not root.exists() and not root.is_symlink()
    resources = WindowsBridgeResources(sid, instance)
    home = storage.home("bridge", instance)
    state = home / "state"
    config = home / "bridge.json"
    pythonw = storage.pinned_pythonw("bridge", instance)
    owner_python = pythonw.with_name("python.exe")
    worker_python = root / "envs/agent-bridge/Scripts/python.exe"
    manifest = Manifest.parse(
        {
            "schema": 2,
            "component": "bridge",
            "instance_id": instance,
            "sid": sid,
            "pythonw": str(pythonw),
            "settings": {"config": str(config), "bridge_python": str(worker_python)},
        }
    )
    scheduler = Scheduler()
    assert scheduler.status(manifest)["registered"] is False
    storage.prepare("bridge", instance)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _private_file(
        config,
        json.dumps(
            {
                "bridge_id": instance,
                "core_grants_enabled": True,
                "lease_key": "alias",
                "socket_path": resources.pipe_name,
                "state_dir": str(state),
                "lock_dir": str(home / "locks"),
                "allowed_peer_sid": sid,
                "enable_fake_runtime": True,
                "workdirs": [
                    {
                        "alias": "Repo",
                        "host_path": str(workspace),
                        "read_only": False,
                        "agent_mode": "workspace-write",
                        "agent_runtimes": ["fake"],
                    }
                ],
            }
        ),
        sid,
    )
    for python in (owner_python, worker_python):
        _run(
            uv,
            "venv",
            "--offline",
            "--no-project",
            "--python",
            str(Path(sys._base_executable).resolve()),
            str(python.parent.parent),
            cwd=ROOT,
            env=env,
        )
    old, new = releases
    _install(old[0], owner_python, sid, uv, env)
    _worker_install(old[1], worker_python, sid, uv, env)
    keys = {core: secrets.token_bytes(32) for core in ("core-a", "core-b")}
    grants = {
        "schema_version": 1,
        "bridge_id": instance,
        "grants": [
            {
                "core_id": core,
                "key_id": "key-" + core[-1],
                "secret_hex": key.hex(),
                "methods": ["bridge.ping", "runtime.list", "task.submit", "task.get", "task.list"],
                "workdirs": [{"alias": "Repo", "policy_sha256": "0" * 64}],
            }
            for core, key in keys.items()
        ],
    }
    result = subprocess.run(
        [str(worker_python), "-I", "-B", "-c", _PROVISION, str(config)],
        input=json.dumps(grants),
        env=bridge_child_environment(None),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, "private Bridge grant provisioning failed"
    digest = result.stdout.strip()
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    grant_file = state / "core-grants.json"
    storage.check_private(grant_file, directory=False, sid=sid)
    storage.stage(manifest)
    protected = {p: p.read_bytes() for p in (config, grant_file, home / "service.json")}
    state_inode = state.stat().st_ino
    retained = {}
    generations = set()
    attempted = False
    passed = False

    def exercise(marker):
        peer_pids = set()
        validate = make_bridge_peer_validator(bridge_id=instance, user_sid=sid)

        def verified(pid):
            if not validate(pid):
                return False
            peer_pids.add(pid)
            return True

        def client(core):
            return CoreGrantBridgeClient(
                Path(resources.pipe_name),
                core_id=core,
                bridge_id=instance,
                key_id="key-" + core[-1],
                secret=keys[core],
                policy_revisions={"Repo": digest},
                server_pid_validator=verified,
                timeout_seconds=3.0,
            )

        async def call():
            own, peer = client("core-a"), client("core-b")
            assert await own.call("bridge.ping", {"workdir": "Repo"}) == {"status": "ok"}
            for task_id, value in retained.items():
                args = {"workdir": "Repo", "task_id": task_id}
                task = await own.call("task.get", args)
                assert task["status"] == "succeeded" and task["final_response"] == value
                with pytest.raises(AgentBridgeRemoteError) as denied:
                    await peer.call("task.get", args)
                assert denied.value.code == "CORE_TASK_NOT_FOUND"
            if marker is not None:
                row = await own.call(
                    "task.submit",
                    {
                        "runtime": "fake",
                        "workdir": "Repo",
                        "path": "",
                        "profile": "workspace-write",
                        "prompt": f"complete:{marker}",
                    },
                )
                task_id = row["task_id"]
                for _ in range(100):
                    task = await own.call("task.get", {"workdir": "Repo", "task_id": task_id})
                    if task["status"] == "succeeded":
                        assert task["final_response"] == marker
                        retained[task_id] = marker
                        break
                    assert task["status"] in {"queued", "running"}
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("installed Bridge synthetic task did not finish")
            history = await own.call("task.list", {"workdir": "Repo", "status": "all"})
            assert {task["task_id"] for task in history["tasks"]} == set(retained)

        asyncio.run(call())
        assert len(peer_pids) == 1
        worker = process_identity(next(iter(peer_pids)))
        assert worker is not None and worker.creation_time > 0
        return worker

    try:
        attempted = True
        installed = scheduler.install(manifest)
        assert not installed["enabled"] and not installed["running"]
        task_xml = home / "task.xml"
        protected[task_xml] = task_xml.read_bytes()
        for index, (release, marker) in enumerate(
            ((old, "historical-result"), (new, "candidate-result"), (old, None))
        ):
            assert _protected_tasks(manifest.task_name()) == prior_tasks
            assert _existing_instances() == prior_instances | {instance}
            assert scheduler.status(manifest)["enabled"] is False
            assert _current_identity(manifest) is None
            if index:
                _install(release[0], owner_python, sid, uv, env)
                _worker_install(release[1], worker_python, sid, uv, env)
            active = scheduler.start(manifest)
            assert active["running"] is True and active["readiness"] == "owner_alive"
            owner = _current_identity(manifest)
            assert owner is not None
            worker = exercise(marker)
            pair = (owner.pid, owner.creation_time, worker.pid, worker.creation_time)
            assert pair not in generations
            generations.add(pair)
            stopped = scheduler.stop(manifest)
            assert not stopped["enabled"] and not stopped["running"]
            assert _current_identity(manifest) is None
            deadline = asyncio.run(_wait_worker_exit(worker))
            assert deadline, "owned Bridge worker did not exit"
            assert state.stat().st_ino == state_inode
            assert all(p.read_bytes() == value for p, value in protected.items())
            assert _protected_tasks(manifest.task_name()) == prior_tasks
        assert len(generations) == 3 and len(retained) == 2
        passed = True
    finally:
        if attempted and scheduler.status(manifest)["registered"]:
            scheduler.stop(manifest)
            assert _current_identity(manifest) is None
            assert scheduler.uninstall(manifest)["registered"] is False
        assert scheduler.status(manifest)["registered"] is False
        assert _protected_tasks() == prior_tasks
        assert _existing_instances() == prior_instances | {instance}
        if passed:
            _remove_owned_root(root, instance, sid)
            assert _existing_instances() == prior_instances
    print("WINDOWS_HISTORICAL_SCHEDULER_BRIDGE_ROLLBACK_PASS", flush=True)


async def _wait_worker_exit(worker) -> bool:
    for _ in range(60):
        actual = process_identity(worker.pid)
        if actual is None or actual.creation_time != worker.creation_time:
            return True
        await asyncio.sleep(0.2)
    return False

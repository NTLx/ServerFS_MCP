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
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from e2e.fixture_memory_policy import fixture_entry_evidence
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
    _exercise_scheduler_bridge(tmp_path, historical=True)


def test_candidate_independent_bridge_scheduler_restart(tmp_path: Path) -> None:
    """Prove installed candidate lifecycle separately from a historical rollback."""
    _exercise_scheduler_bridge(tmp_path, historical=False)


@pytest.mark.skipif(
    os.environ.get("SERVERFS_WINDOWS_REAL_CLAUDE_ACCEPT") != "1",
    reason="explicit single real Claude provider diagnostic required",
)
def test_candidate_independent_bridge_scheduler_real_claude(tmp_path: Path) -> None:
    _exercise_scheduler_bridge(tmp_path, historical=False, real_provider=True)


def _exercise_scheduler_bridge(
    tmp_path: Path, *, historical: bool, real_provider: bool = False
) -> None:
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
    runtime = "claude" if real_provider else "fake"
    claude = shutil.which("claude") if real_provider else None
    if real_provider:
        assert not historical and claude, "native Claude CLI unavailable"
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
    sources = [("new", expected + ":windows")]
    if historical:
        sources.insert(0, ("old", BASELINE))
    for name, tree in sources:
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
    marker_bytes = b"serverfs-v27-provider-fixture\n"
    marker_file = workspace / "marker.txt"
    sibling = tmp_path / "unsubmitted"
    if real_provider:
        marker_file.write_bytes(marker_bytes)
        sibling.mkdir()
        (sibling / "marker.txt").write_bytes(marker_bytes)

    def verify_provider_workspace():
        # Maintainer-approved v0.26.0 acceptance policy: permit ONLY an ordinary
        # local .remember directory in the submitted alias. Never read its contents.
        def ordinary(path, *, directory):
            info = path.lstat()
            return (
                not info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                and (directory or info.st_nlink == 1)
            )

        assert ordinary(workspace, directory=True) and ordinary(sibling, directory=True)
        names = {p.name for p in workspace.iterdir()}
        memory = workspace / ".remember"
        evidence = fixture_entry_evidence(
            {"marker.txt"},
            names,
            memory_allowed=True,
            memory_is_directory=".remember" in names and ordinary(memory, directory=True),
        )
        assert evidence["entries_unchanged"], "provider created an unexpected entry"
        assert ordinary(marker_file, directory=False) and marker_file.read_bytes() == marker_bytes
        assert {p.name for p in sibling.iterdir()} == {"marker.txt"}
        sibling_marker = sibling / "marker.txt"
        assert (
            ordinary(sibling_marker, directory=False)
            and sibling_marker.read_bytes() == marker_bytes
        )
        print(
            f"WINDOWS_PROVIDER_FIXTURE_PASS remember_added={evidence['remember_added']}", flush=True
        )

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
                "enable_fake_runtime": not real_provider,
                **(
                    {"claude": {"enabled": True, "claude_bin": str(Path(claude).resolve())}}
                    if real_provider
                    else {}
                ),
                "workdirs": [
                    {
                        "alias": "Repo",
                        "host_path": str(workspace),
                        "read_only": False,
                        "agent_mode": "workspace-write",
                        "agent_runtimes": [runtime],
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
    first, last = releases[0], releases[-1]
    _install(first[0], owner_python, sid, uv, env)
    _worker_install(first[1], worker_python, sid, uv, env)
    keys = {core: secrets.token_bytes(32) for core in ("core-a", "core-b")}
    grants = {
        "schema_version": 1,
        "bridge_id": instance,
        "grants": [
            {
                "core_id": core,
                "key_id": "key-" + core[-1],
                "secret_hex": key.hex(),
                "methods": [
                    "bridge.ping",
                    "runtime.list",
                    "task.submit",
                    "task.get",
                    "task.list",
                    "task.events",
                    "task.cancel",
                ],
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
                if task["status"] != "succeeded" or task["final_response"] != value:
                    pytest.fail("retained task result mismatch")
                with pytest.raises(AgentBridgeRemoteError) as denied:
                    await peer.call("task.get", args)
                assert denied.value.code == "CORE_TASK_NOT_FOUND"
            if marker is not None:
                row = await own.call(
                    "task.submit",
                    {
                        "runtime": runtime,
                        "workdir": "Repo",
                        "path": "",
                        "profile": "workspace-write",
                        "prompt": (
                            f"Respond with exactly {marker}. Text only. Do not run commands, "
                            "use tools, access files, browse, edit, call MCP servers or request "
                            "permissions. Stop after replying."
                            if real_provider
                            else f"complete:{marker}"
                        ),
                    },
                )
                task_id = row["task_id"]
                try:
                    deadline = asyncio.get_running_loop().time() + (110 if real_provider else 10)
                    while asyncio.get_running_loop().time() < deadline:
                        task = await own.call("task.get", {"workdir": "Repo", "task_id": task_id})
                        if task["status"] == "succeeded":
                            response = task["final_response"]
                            if (
                                not isinstance(response, str)
                                or (response.strip() if real_provider else response) != marker
                            ):
                                pytest.fail("provider marker mismatch")
                            if real_provider:
                                result = task["result"]
                                assert result["storage"] == "inline"
                                assert result["size_bytes"] == len(response.encode())
                                assert (
                                    result["sha256"]
                                    == hashlib.sha256(response.encode()).hexdigest()
                                )
                                assert task["final_response_truncated"] is False
                                cursor, count = 0, 0
                                for _ in range(20):
                                    page = await own.call(
                                        "task.events",
                                        {
                                            "workdir": "Repo",
                                            "task_id": task_id,
                                            "after_event_id": cursor,
                                            "limit": 100,
                                        },
                                    )
                                    events = page["events"]
                                    assert isinstance(events, list)
                                    for event in events:
                                        kind = event["event_type"]
                                        assert isinstance(kind, str)
                                        assert not kind.startswith(
                                            ("tool.", "command.", "file_change.")
                                        )
                                        assert kind != "file.changed"
                                    count += len(events)
                                    if not events:
                                        break
                                    next_cursor = page["next_after_event_id"]
                                    assert type(next_cursor) is int and next_cursor > cursor
                                    cursor = next_cursor
                                else:
                                    pytest.fail("provider event audit exceeded bound")
                                assert count > 0
                            retained[task_id] = response
                            break
                        assert task["status"] in {"queued", "running"}, "provider task refused"
                        await asyncio.sleep(0.5 if real_provider else 0.05)
                    else:
                        pytest.fail("installed Bridge task did not finish")
                finally:
                    if real_provider and task_id not in retained:
                        try:
                            await own.call("task.cancel", {"workdir": "Repo", "task_id": task_id})
                        except AgentBridgeRemoteError:
                            pass
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
        sequence = (
            ((first, "historical-result"), (last, "candidate-result"), (first, None))
            if historical
            else (
                (first, "candidate-first-result"),
                (first, "candidate-second-result"),
                (first, None),
            )
        )
        if real_provider:
            sequence = ((first, "SF_V27_" + secrets.token_hex(12)), (first, None))
        for index, (release, marker) in enumerate(sequence):
            assert _protected_tasks(manifest.task_name()) == prior_tasks
            assert _existing_instances() == prior_instances | {instance}
            assert scheduler.status(manifest)["enabled"] is False
            assert _current_identity(manifest) is None
            if index and historical:
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
            if real_provider:
                verify_provider_workspace()
            assert state.stat().st_ino == state_inode
            assert all(p.read_bytes() == value for p, value in protected.items())
            assert _protected_tasks(manifest.task_name()) == prior_tasks
        assert len(generations) == (2 if real_provider else 3)
        assert len(retained) == (1 if real_provider else 2)
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
    print(
        "WINDOWS_HISTORICAL_SCHEDULER_BRIDGE_ROLLBACK_PASS"
        if historical
        else "WINDOWS_CANDIDATE_REAL_CLAUDE_PASS"
        if real_provider
        else "WINDOWS_CANDIDATE_SCHEDULER_BRIDGE_RESTART_PASS",
        flush=True,
    )


async def _wait_worker_exit(worker) -> bool:
    for _ in range(60):
        actual = process_identity(worker.pid)
        if actual is None or actual.creation_time != worker.creation_time:
            return True
        await asyncio.sleep(0.2)
    return False

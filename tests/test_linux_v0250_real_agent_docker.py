"""Explicit opt-in Docker-to-host-Bridge smoke using the user's real Claude Code runtime."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SRC = ROOT / "agent_bridge" / "src"
if str(BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(BRIDGE_SRC))

from serverfs_agent_bridge.config import BridgeConfig  # noqa: E402
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest  # noqa: E402
from serverfs_agent_bridge.core_grants_private import provision_private_core_grants  # noqa: E402
from serverfs_agent_bridge.store import TaskStore  # noqa: E402

from test_linux_v0250_http_e2e import _http_status, _port, _tool  # noqa: E402

IMAGE = os.environ.get("SERVERFS_LINUX_DOCKER_TEST_IMAGE", "")
REAL_AGENT_ACCEPT = os.environ.get("SERVERFS_LINUX_REAL_AGENT_ACCEPT", "")
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not IMAGE or REAL_AGENT_ACCEPT != "claude",
    reason=("requires SERVERFS_LINUX_REAL_AGENT_ACCEPT=claude and an explicit cached Docker image"),
)


def test_docker_core_submits_one_real_claude_task_via_host_bridge(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    assert os.getuid() != 0
    docker = shutil.which("docker")
    claude = shutil.which("claude")
    uv = shutil.which("uv")
    assert docker is not None and claude is not None and uv is not None
    daemon = [docker, "--host", "unix:///var/run/docker.sock"]
    project = "serverfs-v0250-agent-" + uuid.uuid4().hex[:12]
    root = tmp_path.resolve()
    root.chmod(0o700)
    # pytest preserves tmp_path directories between runs; this tree contains synthetic grant keys.
    request.addfinalizer(lambda: shutil.rmtree(root, ignore_errors=True))
    bridge_root = root / "bridge"
    socket_dir, state, locks = (bridge_root / part for part in ("run", "state", "locks"))
    binding_dir = root / "binding"
    workdir = root / "work"
    disabled = root / "disabled"
    for directory in (bridge_root, socket_dir, state, locks, binding_dir, workdir, disabled):
        directory.mkdir(mode=0o700)
    (disabled / ".serverfs-disabled").touch()
    marker = "SERVERFS_LOCAL_READ_ONLY_ACCEPT_" + uuid.uuid4().hex
    fixture = workdir / "fixture.txt"
    fixture.write_text(marker + "\n", encoding="utf-8")
    before_files = {path.name for path in workdir.iterdir()}
    api_key = "synthetic-" + uuid.uuid4().hex
    core_id = "core-claude-" + uuid.uuid4().hex[:8]
    bridge_id = "bridge-claude-" + uuid.uuid4().hex[:8]
    key_id = "key-" + uuid.uuid4().hex[:12]
    secret = os.urandom(32)
    port = _port()
    config_path = root / "bridge-config.json"
    config_document = {
        "protocol_mode": "v2",
        "bridge_id": bridge_id,
        "lease_key": "alias",
        "socket_path": str(socket_dir / "bridge.sock"),
        "state_dir": str(state),
        "lock_dir": str(locks),
        "allowed_peer_uid": os.getuid(),
        "allowed_peer_gid": os.getgid(),
        "enable_fake_runtime": False,
        # Force the small synthetic marker into the durable result spool so MCP client B can
        # verify the separate task.result.read path after discovering the task.
        "limits": {"result_spool_threshold_bytes": 1},
        "claude": {"enabled": True, "claude_bin": claude, "use_proxy": False},
        "workdirs": [
            {
                "slot": None,
                "alias": "fixture",
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": ["claude"],
                "repo_ops_enabled": False,
            }
        ],
    }
    config_path.write_text(json.dumps(config_document), encoding="utf-8")
    config_path.chmod(0o600)
    bridge_config = BridgeConfig.load(config_path)
    policy_digest = core_workdir_policy_digest(bridge_config.policies.get("fixture"))
    store = TaskStore(state)
    grant_document = {
        "schema_version": 1,
        "bridge_id": bridge_id,
        "grants": [
            {
                "core_id": core_id,
                "key_id": key_id,
                "secret_hex": secret.hex(),
                "methods": [
                    "bridge.ping",
                    "runtime.list",
                    "task.submit",
                    "task.get",
                    "task.list",
                    "task.events",
                    "task.result.read",
                    "task.cancel",
                ],
                "workdirs": [{"alias": "fixture", "policy_sha256": policy_digest}],
            }
        ],
    }
    provision_private_core_grants(
        store=store,
        bridge_id=bridge_id,
        policies=bridge_config.policies,
        document=grant_document,
    )
    binding = {
        "schema_version": 1,
        "instance_id": core_id,
        "core_id": core_id,
        "bridge_id": bridge_id,
        "key_id": key_id,
        "secret_hex": secret.hex(),
        "workdir_alias": "fixture",
        "workdir_path": "/workdirs/01",
        "policy_revision": policy_digest,
        "socket_path": "/run/serverfs-agent-bridge/bridge.sock",
    }
    binding_path = binding_dir / "core-bridge-binding.json"
    binding_path.write_text(json.dumps(binding, sort_keys=True), encoding="utf-8")
    binding_path.chmod(0o400)

    empty_env = root / "empty.env"
    empty_env.touch(mode=0o600)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(Path.home()),
        "SERVERFS_IMAGE": IMAGE,
        "SERVERFS_UID": str(os.getuid()),
        "SERVERFS_GID": str(os.getgid()),
        "SERVERFS_HTTP_HOST": "0.0.0.0",
        "SERVERFS_HTTP_PORT": "8000",
        "SERVERFS_HTTP_PUBLISH_HOST": "127.0.0.1",
        "SERVERFS_HTTP_PUBLISH_PORT": str(port),
        "SERVERFS_HTTP_ALLOWED_HOSTS": f"127.0.0.1:{port}",
        "SERVERFS_HTTP_API_KEY": api_key,
        "SERVERFS_LINUX_INSTANCE_ID": core_id,
        "SERVERFS_LINUX_BRIDGE_ID": bridge_id,
        "SERVERFS_LINUX_CORE_BINDING_DIR": str(binding_dir),
        "SERVERFS_LINUX_BRIDGE_SOCKET_DIR": str(socket_dir),
        "SERVERFS_LINUX_BRIDGE_LOCK_DIR": str(locks),
        "SERVERFS_AGENT_MODE": "disabled",
        "SERVERFS_AGENT_RUNTIMES": "",
        "WORKDIR_01_ALIAS": "fixture",
        "WORKDIR_01_PATH": str(workdir),
        "WORKDIR_01_READ_ONLY": "false",
        "WORKDIR_01_AGENT_MODE": "workspace-write",
        "WORKDIR_01_AGENT_RUNTIMES": "claude",
        "WORKDIR_01_REPO_OPS_ENABLED": "false",
    }
    for slot in range(2, 17):
        env[f"WORKDIR_{slot:02d}_PATH"] = str(disabled)
    files = [
        ROOT / "compose.yml",
        ROOT / "compose.http.yml",
        ROOT / "deployment/linux/compose.publish-http.yml",
        ROOT / "deployment/linux/compose.bridge-v2.yml",
    ]
    base = [*daemon, "compose", "--env-file", str(empty_env), "-p", project]
    for path in files:
        base.extend(("-f", str(path)))

    bridge_env = os.environ.copy()
    bridge_env["PATH"] = os.environ.get("PATH", os.defpath)
    bridge_env["HOME"] = str(Path.home())
    bridge_env["PYTHONDONTWRITEBYTECODE"] = "1"
    bridge_command = [
        uv,
        "run",
        "--project",
        str(ROOT / "agent_bridge"),
        "--no-sync",
        "python",
        "-m",
        "serverfs_agent_bridge.main",
        "--config",
        str(config_path),
    ]
    bridge: subprocess.Popen[bytes] | None = None
    task_id: str | None = None

    def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [*base, *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if check:
            assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
        return result

    def call(name: str, arguments: dict | None = None):
        return asyncio.run(_tool(port, api_key, name, arguments))

    def cancel_and_wait() -> None:
        nonlocal task_id
        if task_id is None or _http_status(port) != 401:
            return
        try:
            call("cancel_agent_task", {"task_id": task_id})
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                result = call("get_agent_task", {"task_id": task_id})
                data = result.structured_content or {}
                if data.get("status") in {"succeeded", "failed", "cancelled", "interrupted"}:
                    return
                time.sleep(0.5)
        except Exception:
            return

    try:
        assert not compose("ps", "-a", "-q").stdout.strip()
        bridge = subprocess.Popen(
            bridge_command,
            cwd=ROOT,
            env=bridge_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        socket_path = socket_dir / "bridge.sock"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if bridge.poll() is not None:
                pytest.fail("temporary host Bridge exited before startup")
            if socket_path.is_socket():
                break
            time.sleep(0.1)
        else:
            pytest.fail("temporary host Bridge socket did not appear")
        socket_info = socket_path.stat()
        assert socket_info.st_uid == os.getuid()
        assert stat.S_IMODE(socket_info.st_mode) & 0o007 == 0

        compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "serverfs-mcp")
        container = compose("ps", "-q", "serverfs-mcp").stdout.strip()
        assert container
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _http_status(port) == 401:
                break
            time.sleep(0.2)
        else:
            pytest.fail("Docker Core did not become ready after signed Bridge startup")

        runtimes = call("list_agent_runtimes")
        available = any(
            item.get("name") == "claude" and item.get("available") is True
            for item in (runtimes.structured_content or {}).get("runtimes", [])
        )
        assert available, "Claude Code was unavailable through the isolated host Bridge"

        submitted = call(
            "submit_agent_task",
            {
                "runtime": "claude",
                "workdir": "fixture",
                "path": "",
                "profile": "workspace-write",
                "prompt": (
                    "Read only fixture.txt in the current directory. Do not modify any file, "
                    "do not use network or other tools, and do not access anything outside "
                    "this workdir. Return exactly the single marker text found in the file, "
                    "with no explanation."
                ),
            },
        )
        assert not submitted.is_error, "Claude Agent task submission was denied by the Bridge"
        submitted_id = (submitted.structured_content or {}).get("task_id")
        assert isinstance(submitted_id, str) and submitted_id
        # Client A discards the only returned handle before client B starts. Do not retain it in
        # cleanup state; B must learn the task ID exclusively from the public scoped list.
        del submitted, submitted_id

        recovered = None
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            page = call("list_agent_tasks", {"workdir": "fixture", "status": "all"})
            assert not page.is_error, "fresh MCP client could not list scoped Agent tasks"
            tasks = (page.structured_content or {}).get("tasks", [])
            matches = [
                item
                for item in tasks
                if item.get("runtime") == "claude" and item.get("workdir") == "fixture"
            ]
            if matches:
                recovered = matches[0]
                break
            time.sleep(1)
        else:
            pytest.fail("Claude task did not appear in client B's scoped list within 150 seconds")

        task_id = recovered.get("task_id")
        assert isinstance(task_id, str) and task_id
        deadline = time.monotonic() + 150
        task = {}
        while time.monotonic() < deadline:
            task_result = call("get_agent_task", {"task_id": task_id})
            assert not task_result.is_error, "client B could not get its discovered task"
            task = task_result.structured_content or {}
            if task.get("status") in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            if task.get("status") in {"waiting_for_approval", "waiting_for_question"}:
                call("cancel_agent_task", {"task_id": task_id})
                pytest.fail("Claude required human approval/input; task canceled without approval")
            time.sleep(1)
        else:
            call("cancel_agent_task", {"task_id": task_id})
            pytest.fail("Claude task did not reach a terminal result within 150 seconds")

        events = call("read_agent_task_events", {"task_id": task_id, "limit": 100})
        assert not events.is_error and (events.structured_content or {}).get("events"), (
            "client B could not read normalized task events"
        )
        result_page = call(
            "read_agent_task_result", {"task_id": task_id, "offset_bytes": 0, "max_bytes": 256}
        )
        assert not result_page.is_error, "client B could not read the spooled task result"
        result_data = result_page.structured_content or {}
        result_text = result_data.get("text", result_data.get("content", ""))

        assert task.get("status") == "succeeded", (
            "Claude provider task did not succeed; "
            f"terminal_status={task.get('status')} error_code={task.get('error_code')}"
        )
        assert marker in str(result_text) or marker in task.get("final_response", ""), (
            "discovered task result did not contain the synthetic fixture marker"
        )
        assert fixture.read_text(encoding="utf-8") == marker + "\n"
        assert {path.name for path in workdir.iterdir()} == before_files
        print(
            "linux_host_bridge_recovery "
            f"runtime=claude workdir=fixture status={task['status']} "
            f"marker={marker} events={len((events.structured_content or {}).get('events', []))} "
            f"result_storage=spool"
        )

        # The Core is disposable and the Bridge/state remain separate host processes. Restart
        # only this temporary Core, then prove its fresh MCP session can rediscover the retained
        # terminal task from the same scoped list.
        compose("restart", "serverfs-mcp")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _http_status(port) == 401:
                break
            time.sleep(0.2)
        else:
            pytest.fail("temporary Core did not return after isolated restart")
        after_restart = call("list_agent_tasks", {"workdir": "fixture", "status": "terminal"})
        assert not after_restart.is_error, "client B could not list tasks after Core restart"
        retained = [
            item
            for item in (after_restart.structured_content or {}).get("tasks", [])
            if item.get("task_id") == task_id
        ]
        assert retained and retained[0].get("status") == "succeeded", (
            "terminal task was not recoverable from the scoped list after Core restart"
        )
        after_restart_get = call("get_agent_task", {"task_id": retained[0]["task_id"]})
        assert not after_restart_get.is_error
        assert (after_restart_get.structured_content or {}).get("status") == "succeeded"
        print("linux_host_bridge_recovery core_restart=PASS retained_status=succeeded")

        # Restart only this disposable Bridge owner, preserving its private state, then use a
        # fresh HTTP MCP request to rediscover and read the same retained terminal task.
        bridge.terminate()
        bridge.wait(timeout=10)
        bridge = subprocess.Popen(
            bridge_command,
            cwd=ROOT,
            env=bridge_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if bridge.poll() is not None:
                pytest.fail("temporary host Bridge owner exited after restart")
            if socket_path.is_socket():
                break
            time.sleep(0.1)
        else:
            pytest.fail("temporary host Bridge socket did not return after owner restart")
        bridge_restart_page = call("list_agent_tasks", {"workdir": "fixture", "status": "terminal"})
        assert not bridge_restart_page.is_error, (
            "fresh MCP client could not list tasks after Bridge owner restart"
        )
        bridge_retained = [
            item
            for item in (bridge_restart_page.structured_content or {}).get("tasks", [])
            if item.get("task_id") == task_id
        ]
        assert bridge_retained and bridge_retained[0].get("status") == "succeeded", (
            "terminal task was not rediscoverable after Bridge owner restart"
        )
        bridge_restart_get = call("get_agent_task", {"task_id": task_id})
        assert not bridge_restart_get.is_error
        assert (bridge_restart_get.structured_content or {}).get("status") == "succeeded"
        print("linux_host_bridge_recovery bridge_owner_restart=PASS retained_status=succeeded")
    finally:
        cancel_and_wait()
        compose("down", "--timeout", "5", "--remove-orphans", check=False)
        if bridge is not None and bridge.poll() is None:
            bridge.terminate()
            try:
                bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bridge.kill()
                bridge.wait(timeout=5)
        containers = subprocess.run(
            [*daemon, "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        networks = subprocess.run(
            [
                *daemon,
                "network",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert not containers.stdout.strip(), f"test project containers remain: {containers.stdout}"
        assert not networks.stdout.strip(), f"test project networks remain: {networks.stdout}"

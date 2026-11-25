"""Native-process Linux v2 Core HTTP smoke using one isolated local Bridge.

The native test injects a temporary registry root through the existing builder argument.
Production ``/workdirs`` mount behavior remains a separate Docker-path acceptance check.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only v2 HTTP acceptance")

_ROOT = Path(__file__).resolve().parents[1]
_BRIDGE_SRC = _ROOT / "agent_bridge" / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))

from serverfs_agent_bridge.adapters import FakeAdapter  # noqa: E402
from serverfs_agent_bridge.core_grants import (  # noqa: E402
    CoreGrant,
    CoreGrantVerifier,
    core_workdir_policy_digest,
)
from serverfs_agent_bridge.lease_identity import alias_lease_id  # noqa: E402
from serverfs_agent_bridge.leases import LeaseManager  # noqa: E402
from serverfs_agent_bridge.models import AgentMode  # noqa: E402
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy  # noqa: E402
from serverfs_agent_bridge.protocol import BridgeProtocolServer  # noqa: E402
from serverfs_agent_bridge.service import BridgeService  # noqa: E402
from serverfs_agent_bridge.store import TaskStore  # noqa: E402


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _http_status(port: int, token: str | None = None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", headers=headers)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=0.5
        ) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (TimeoutError, OSError):
        return 0


async def _wait_ready(child: subprocess.Popen[bytes], port: int, log: Path) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise AssertionError(
                f"Core exited ({child.returncode}): {log.read_text(errors='replace')[-1000:]}"
            )
        if await asyncio.to_thread(_http_status, port) == 401:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("Core HTTP readiness timed out")


async def _tool(port: int, token: str, name: str, arguments: dict | None = None):
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=8, trust_env=False
    ) as http:
        transport = streamable_http_client(
            f"http://127.0.0.1:{port}/mcp", http_client=http, terminate_on_close=False
        )
        async with Client(transport, mode="2026-07-28", raise_exceptions=True) as client:
            if name == "tools/list":
                return {item.name for item in (await client.list_tools()).tools}
            return await client.call_tool(name, arguments or {})


def _private_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def test_two_linux_core_http_processes_use_one_signed_v2_bridge() -> None:
    with tempfile.TemporaryDirectory(prefix="serverfs-linux-v0250-") as scratch:
        root = Path(scratch).resolve(strict=True)
        root.chmod(0o700)
        workdir_roots = (root / "core-alpha-workdirs", root / "core-bravo-workdirs")
        roots = tuple(workdir_root / "01" for workdir_root in workdir_roots)
        for workdir_root, path in zip(workdir_roots, roots, strict=True):
            workdir_root.mkdir(mode=0o700)
            path.mkdir(mode=0o700)
            (path / "task-context").mkdir(mode=0o700)
            (path / "hello.txt").write_text("isolated", encoding="utf-8")
            for slot in range(2, 17):
                disabled = workdir_root / f"{slot:02d}"
                disabled.mkdir(mode=0o700)
                (disabled / ".serverfs-disabled").touch()

        bridge_id = "linux-v0250-smoke"
        socket_path = root / "bridge.sock"
        state, locks = root / "bridge-state", root / "locks"
        state.mkdir(mode=0o700)
        policies = [
            WorkdirAgentPolicy(
                slot=None,
                alias=alias,
                host_path=path,
                mode=AgentMode.WORKSPACE_WRITE,
                # Core's public config intentionally excludes the test-only runtime name. Map
                # the public codex policy to the deterministic fake adapter for this harness.
                runtimes=frozenset({"codex"}),
                read_only=False,
            )
            for alias, path in zip(("Alpha", "Bravo"), roots, strict=True)
        ]
        digests = [core_workdir_policy_digest(policy) for policy in policies]
        store = TaskStore(state)
        service = BridgeService(
            store=store,
            policies=PolicyRegistry(policies),
            adapters={"codex": FakeAdapter()},
            lease_manager=LeaseManager(
                locks, lease_ids=[alias_lease_id(policy.alias) for policy in policies]
            ),
        )
        secrets = (bytes(range(32)), bytes(range(32, 64)))
        core_ids = ("core-alpha", "core-bravo")
        key_ids = ("key-alpha", "key-bravo")
        grants = tuple(
            CoreGrant(
                core_id,
                key_id,
                secret,
                ((policy.alias, digest),),
                frozenset(
                    {
                        "bridge.ping",
                        "runtime.list",
                        "task.submit",
                        "task.list",
                        "task.get",
                        "task.events",
                        "task.result.read",
                        "task.approval.respond",
                        "task.question.answer",
                    }
                ),
            )
            for core_id, key_id, secret, policy, digest in zip(
                core_ids, key_ids, secrets, policies, digests, strict=True
            )
        )
        bridge = BridgeProtocolServer(
            service=service,
            socket_path=socket_path,
            allowed_peer_uid=os.getuid(),
            v2_only=True,
            core_grant_verifier=CoreGrantVerifier(
                bridge_id, grants, nonce_claim=store.claim_core_nonce
            ),
        )
        ports = (_port(), _port())
        while ports[0] == ports[1]:
            ports = (_port(), _port())
        tokens = ("local-smoke-alpha-key", "local-smoke-bravo-key")

        async def exercise() -> None:
            await bridge.start()
            try:
                assert socket_path.is_socket()
                with ExitStack() as stack:
                    children = []
                    launch_specs = []
                    for index, (alias, core_id) in enumerate(
                        zip(("Alpha", "Bravo"), core_ids, strict=True)
                    ):
                        binding_dir = root / f"binding-{core_id}"
                        binding_dir.mkdir(mode=0o700)
                        binding = binding_dir / "core-bridge-binding.json"
                        _private_json(
                            binding,
                            {
                                "schema_version": 1,
                                "instance_id": core_id,
                                "core_id": core_id,
                                "bridge_id": bridge_id,
                                "key_id": key_ids[index],
                                "secret_hex": secrets[index].hex(),
                                "workdir_alias": alias,
                                "workdir_path": str(roots[index]),
                                "policy_revision": digests[index],
                                "socket_path": str(socket_path),
                            },
                        )
                        env = {
                            "HOME": str(root),
                            "PATH": os.defpath,
                            "PYTHONPATH": str(_ROOT / "src"),
                            "PYTHONDONTWRITEBYTECODE": "1",
                            "SERVERFS_HTTP_API_KEY": tokens[index],
                            "SERVERFS_HTTP_ALLOWED_HOSTS": f"127.0.0.1:{ports[index]}",
                            "SERVERFS_HTTP_PORT": str(ports[index]),
                            "SERVERFS_AGENT_BRIDGE_ENABLED": "true",
                            "SERVERFS_AGENT_BRIDGE_SOCKET": str(socket_path),
                            "SERVERFS_AGENT_LOCK_DIR": str(locks),
                            "SERVERFS_LINUX_V25_BRIDGE_BINDING_FILE": str(binding),
                            "SERVERFS_LINUX_INSTANCE_ID": core_id,
                            "SERVERFS_LINUX_BRIDGE_ID": bridge_id,
                            "WORKDIR_01_ALIAS": alias,
                            "WORKDIR_01_READ_ONLY": "false",
                            "WORKDIR_01_AGENT_MODE": "workspace-write",
                            "WORKDIR_01_AGENT_RUNTIMES": "codex",
                        }
                        for slot in range(1, 17):
                            env.setdefault(f"WORKDIR_{slot:02d}_READ_ONLY", "true")
                        # main.main is real; inject only the temporary workdir root through the
                        # existing builder argument, since production Linux uses /workdirs.
                        runner = root / f"run-{core_id}.py"
                        runner.write_text(
                            "from functools import partial\n"
                            "from pathlib import Path\n"
                            "import serverfs_mcp.main as main\n"
                            "from serverfs_mcp.workdirs import build_registry_from_env\n"
                            "main.build_registry_from_env = partial(\n"
                            "    build_registry_from_env,\n"
                            f"    workdir_root=Path({str(workdir_roots[index])!r}),\n"
                            ")\n"
                            "raise SystemExit(main.main())\n",
                            encoding="utf-8",
                        )
                        log = root / f"{core_id}.stderr"
                        stream = stack.enter_context(log.open("wb"))
                        child = subprocess.Popen(
                            [sys.executable, "-B", str(runner)],
                            cwd=root,
                            env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=stream,
                            start_new_session=True,
                        )

                        def stop(process=child) -> None:
                            if process.poll() is None:
                                process.terminate()
                                try:
                                    process.wait(timeout=4)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    process.wait(timeout=4)

                        stack.callback(stop)
                        children.append((child, log))
                        launch_specs.append((runner, env, stream))

                    for index, (child, log) in enumerate(children):
                        await _wait_ready(child, ports[index], log)
                        assert (
                            await asyncio.to_thread(_http_status, ports[index], tokens[1 - index])
                            == 401
                        )
                        assert "submit_agent_task" in await _tool(
                            ports[index], tokens[index], "tools/list"
                        )
                        file_result = await _tool(
                            ports[index],
                            tokens[index],
                            "read_text_file",
                            {"workdir": ("Alpha", "Bravo")[index], "path": "hello.txt"},
                        )
                        assert not file_result.is_error

                    async def restart_core(index: int) -> None:
                        old_child, log = children[index]
                        runner, env, stream = launch_specs[index]
                        bridge_identity = socket_path.stat()
                        stop(old_child)
                        assert old_child.poll() is not None
                        child = subprocess.Popen(
                            [sys.executable, "-B", str(runner)],
                            cwd=root,
                            env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=stream,
                            start_new_session=True,
                        )
                        stack.callback(stop, child)
                        children[index] = (child, log)
                        await _wait_ready(child, ports[index], log)
                        current_identity = socket_path.stat()
                        assert (bridge_identity.st_dev, bridge_identity.st_ino) == (
                            current_identity.st_dev,
                            current_identity.st_ino,
                        )
                        assert child.pid != old_child.pid
                        assert (
                            await asyncio.to_thread(_http_status, ports[index], tokens[1 - index])
                            == 401
                        )

                    wrong_auth = await asyncio.to_thread(_http_status, ports[0], "not-the-key")
                    assert wrong_auth == 401
                    result = await _tool(
                        ports[0],
                        tokens[0],
                        "submit_agent_task",
                        {
                            "runtime": "codex",
                            "workdir": "Alpha",
                            "path": "task-context",
                            "profile": "workspace-write",
                            "prompt": "local synthetic smoke task",
                        },
                    )
                    assert not result.is_error and result.structured_content, result.content
                    task_id = result.structured_content["task_id"]
                    fetched = await _tool(
                        ports[0], tokens[0], "get_agent_task", {"task_id": task_id}
                    )
                    assert not fetched.is_error and fetched.structured_content["task_id"] == task_id
                    denied = await _tool(
                        ports[1], tokens[1], "get_agent_task", {"task_id": task_id}
                    )
                    assert denied.is_error

                    async def discover(core_index: int, status: str, expected: str) -> dict:
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            page = await _tool(
                                ports[core_index],
                                tokens[core_index],
                                "list_agent_tasks",
                                {"workdir": ("Alpha", "Bravo")[core_index], "status": status},
                            )
                            assert not page.is_error, page.content
                            matches = [
                                task
                                for task in page.structured_content["tasks"]
                                if task["status"] == expected
                            ]
                            if matches:
                                return matches[0]
                            await asyncio.sleep(0.05)
                        raise AssertionError(f"task did not appear as {expected} in {status} list")

                    # A is one authenticated MCP client/session; every _tool call below opens a
                    # fresh authenticated MCP session. B receives no task ID from A and learns
                    # the handle only from its own list_agent_tasks response.
                    approval_submit = await _tool(
                        ports[0],
                        tokens[0],
                        "submit_agent_task",
                        {
                            "runtime": "codex",
                            "workdir": "Alpha",
                            "path": "task-context",
                            "profile": "workspace-write",
                            "prompt": "approval: isolated discovery fixture",
                        },
                    )
                    assert not approval_submit.is_error, approval_submit.content
                    del approval_submit
                    approval_task = await discover(0, "active", "waiting_for_approval")
                    assert approval_task["task_id"] != ""
                    assert approval_task["pending_interaction"] == "approval"
                    await restart_core(0)
                    rediscovered = await discover(0, "active", "waiting_for_approval")
                    assert rediscovered["task_id"] == approval_task["task_id"]
                    assert rediscovered["pending_interaction"] == "approval"
                    approval_task = rediscovered
                    # While A's task owns Alpha's writer lease, B recovers it via discovery and
                    # performs only existing read/interaction operations using that discovered ID.
                    approval_id = approval_task["task_id"]
                    lease_write = await _tool(
                        ports[0],
                        tokens[0],
                        "create_text_file",
                        {"workdir": "Alpha", "path": "lease-probe.txt", "content": "no"},
                    )
                    assert lease_write.is_error and "WORKDIR_BUSY" in str(lease_write.content)
                    assert not (roots[0] / "lease-probe.txt").exists()
                    foreign_page = await _tool(
                        ports[1], tokens[1], "list_agent_tasks", {"workdir": "Bravo"}
                    )
                    assert not foreign_page.is_error
                    assert all(
                        task["task_id"] != approval_id
                        for task in foreign_page.structured_content["tasks"]
                    )
                    foreign_get = await _tool(
                        ports[1], tokens[1], "get_agent_task", {"task_id": approval_id}
                    )
                    assert foreign_get.is_error
                    recovered = await _tool(
                        ports[0], tokens[0], "get_agent_task", {"task_id": approval_id}
                    )
                    assert not recovered.is_error and recovered.structured_content["status"] == (
                        "waiting_for_approval"
                    )
                    event_page = await _tool(
                        ports[0],
                        tokens[0],
                        "read_agent_task_events",
                        {"task_id": approval_id, "limit": 50},
                    )
                    assert not event_page.is_error and event_page.structured_content["events"]
                    request = recovered.structured_content["pending_request"]
                    resolved = await _tool(
                        ports[0],
                        tokens[0],
                        "respond_agent_approval",
                        {
                            "task_id": approval_id,
                            "request_id": request["request_id"],
                            "decision": "approve_once",
                        },
                    )
                    assert not resolved.is_error and resolved.structured_content["resolved"]
                    terminal = await discover(0, "terminal", "succeeded")
                    assert terminal["task_id"] == approval_id
                    completed = await _tool(
                        ports[0], tokens[0], "get_agent_task", {"task_id": terminal["task_id"]}
                    )
                    assert not completed.is_error
                    assert completed.structured_content["final_response"] == "approval=approve_once"

                    # The task ID returned to A was discarded; the independently authenticated
                    # Bravo Core cannot discover or operate Alpha's task.
                    question_submit = await _tool(
                        ports[0],
                        tokens[0],
                        "submit_agent_task",
                        {
                            "runtime": "codex",
                            "workdir": "Alpha",
                            "path": "task-context",
                            "profile": "workspace-write",
                            "prompt": "question: deterministic recovery fixture",
                        },
                    )
                    assert not question_submit.is_error, question_submit.content
                    del question_submit
                    question_task = await discover(0, "active", "waiting_for_question")
                    assert question_task["pending_interaction"] == "question"
                    await restart_core(0)
                    rediscovered = await discover(0, "active", "waiting_for_question")
                    assert rediscovered["task_id"] == question_task["task_id"]
                    assert rediscovered["pending_interaction"] == "question"
                    question_id = rediscovered["task_id"]
                    question_read = await _tool(
                        ports[0], tokens[0], "get_agent_task", {"task_id": question_id}
                    )
                    question_request = question_read.structured_content["pending_request"]
                    answered = await _tool(
                        ports[0],
                        tokens[0],
                        "answer_agent_question",
                        {
                            "task_id": question_id,
                            "request_id": question_request["request_id"],
                            "answers": [
                                {
                                    "question_id": "q1",
                                    "selected_option_ids": ["b"],
                                    "text": "option B",
                                }
                            ],
                        },
                    )
                    assert not answered.is_error and answered.structured_content["resolved"]
                    question_terminal = await discover(0, "terminal", "succeeded")
                    assert question_terminal["task_id"] == question_id
                    question_result = await _tool(
                        ports[0],
                        tokens[0],
                        "get_agent_task",
                        {"task_id": question_terminal["task_id"]},
                    )
                    assert not question_result.is_error
                    assert (
                        '"selected_option_ids": ["b"]'
                        in question_result.structured_content["final_response"]
                    )

            finally:
                await bridge.close()
            assert not socket_path.exists()

        asyncio.run(exercise())

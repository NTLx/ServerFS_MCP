"""Opt-in Linux Docker/host Bridge acceptance; never uses deployed projects or credentials."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from test_linux_v0250_http_e2e import (
    AgentMode,
    BridgeProtocolServer,
    BridgeService,
    CoreGrant,
    CoreGrantVerifier,
    FakeAdapter,
    LeaseManager,
    PolicyRegistry,
    TaskStore,
    WorkdirAgentPolicy,
    _http_status,
    _port,
    _private_json,
    _tool,
    alias_lease_id,
    core_workdir_policy_digest,
)

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("SERVERFS_LINUX_DOCKER_TEST_IMAGE", "")
USE_SOURCE = os.environ.get("SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE") == "1"
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not IMAGE,
    reason="requires explicit local Docker acceptance image",
)

# Probe the same configuration and signed handshake as main(), but only report
# fixed stages and numerical mount permissions. No binding contents are printed.
_PROBE = """
import asyncio
import os
import stat
from pathlib import Path
from serverfs_mcp.config import settings_from_env
from serverfs_mcp.workdirs import build_registry_from_env
from serverfs_mcp.linux_v0250_core_binding import load_linux_core_client, verify_linux_core_client
settings = settings_from_env(os.environ)
print("core uid/gid:", os.getuid(), os.getgid(), flush=True)
path = Path(settings.linux_v0250_bridge_binding_file)
for label, target in (("binding directory", path.parent), ("binding file", path),
                      ("socket", Path(settings.agent_bridge_socket)),
                      ("lease directory", Path(settings.agent_lock_dir))):
    info = target.stat()
    print(label, "uid/gid", info.st_uid, info.st_gid, "mode",
          oct(stat.S_IMODE(info.st_mode)), flush=True)
registry = build_registry_from_env(os.environ, settings)
client, _ = load_linux_core_client(
    str(path), instance_id=os.environ["SERVERFS_LINUX_INSTANCE_ID"],
    bridge_id=os.environ["SERVERFS_LINUX_BRIDGE_ID"],
    socket_path=settings.agent_bridge_socket, workdirs=registry.all_workdirs())
print("private binding accepted", flush=True)
asyncio.run(verify_linux_core_client(client, Path(settings.agent_bridge_socket)))
print("signed ping accepted", flush=True)
"""


@pytest.mark.parametrize(
    ("core_count", "repo_full"),
    [(1, False), (2, False), (2, True)],
    ids=["owned", "shared-multi-workdir", "repo-only-four-tools"],
)
def test_docker_core_with_host_v2_bridge(core_count: int, repo_full: bool, monkeypatch) -> None:
    from serverfs_agent_bridge.policy import RepoActionPolicy

    assert os.getuid() != 0
    docker = shutil.which("docker")
    git = shutil.which("git")
    assert docker is not None and git is not None
    daemon = [docker, "--host", "unix:///var/run/docker.sock"]
    with tempfile.TemporaryDirectory(prefix="serverfs-docker-v25-") as raw_root:
        root = Path(raw_root).resolve()
        root.chmod(0o700)
        disabled, socket_dir, state, locks = (
            root / name for name in ("disabled", "socket", "state", "locks")
        )
        for directory in (disabled, socket_dir, state):
            directory.mkdir(mode=0o700)
        (disabled / ".serverfs-disabled").touch()
        bridge_id = "bridge-test"
        aliases = ("Alpha", "Bravo")[:core_count]
        policies = []
        grants = []
        cores = []
        env_file = root / "empty.env"
        env_file.touch(mode=0o600)
        files = [
            ROOT / "compose.yml",
            ROOT / "compose.http.yml",
            ROOT / "deployment/linux/compose.publish-http.yml",
            ROOT / "deployment/linux/compose.bridge-v2.yml",
        ]
        # Keep the cached runtime image immutable when testing current source offline.
        if os.environ.get("SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE") == "1":
            source = root / "candidate-src"
            shutil.copytree(
                ROOT / "src",
                source,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            overlay = root / "source.yml"
            overlay.write_text(
                "services:\n  serverfs-mcp:\n    environment:\n"
                "      PYTHONPATH: /run/serverfs-candidate-src\n"
                "    volumes:\n      - type: bind\n"
                f'        source: "{source}"\n'
                "        target: /run/serverfs-candidate-src\n"
                "        read_only: true\n"
                "        bind:\n          create_host_path: false\n",
                encoding="utf-8",
            )
            files.append(overlay)

        path_env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(root)}
        git_env = {
            **path_env,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        if repo_full:
            # Repo Ops uses the Bridge process HOME; isolate it from user Git configuration.
            monkeypatch.setenv("HOME", str(root))
            monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "git-config"))
        for alias in aliases:
            core_id = "core-" + alias.lower()
            key_id = "key-" + alias.lower()
            work, binding = root / alias, root / f"binding-{core_id}"
            for directory in (work, binding):
                directory.mkdir(mode=0o700)
            (work / "hello.txt").write_text(alias, encoding="utf-8")
            (work / "task-context").mkdir()
            # Real deterministic Repo Ops against a local synthetic Git repository.
            for args in (
                ["init", "-q", "-b", core_id],
                ["add", "hello.txt"],
                [
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-qm",
                    "fixture",
                ],
            ):
                subprocess.run(
                    [git, *args],
                    cwd=work,
                    env=git_env,
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
            policy = WorkdirAgentPolicy(
                slot=None,
                alias=alias,
                host_path=work,
                mode=AgentMode.WORKSPACE_WRITE,
                runtimes=frozenset({"codex"}),
                read_only=False,
                repo_ops_enabled=True,
            )
            digest = core_workdir_policy_digest(policy)
            policies.append(policy)
            secret = secrets.token_bytes(32)
            token = secrets.token_hex(24)
            grants.append(
                CoreGrant(
                    core_id,
                    key_id,
                    secret,
                    ((alias, digest),),
                    frozenset(
                        {
                            "bridge.ping",
                            "runtime.list",
                            "task.submit",
                            "task.get",
                            "task.list",
                            "repo.inspect",
                        }
                    ),
                )
            )
            _private_json(
                binding / "core-bridge-binding.json",
                {
                    "schema_version": 1,
                    "instance_id": core_id,
                    "core_id": core_id,
                    "bridge_id": bridge_id,
                    "key_id": key_id,
                    "secret_hex": secret.hex(),
                    "workdir_alias": alias,
                    "workdir_path": "/workdirs/01",
                    "policy_revision": digest,
                    "socket_path": "/run/serverfs-agent-bridge/bridge.sock",
                },
            )
            extra_workdirs = {}
            if core_count == 2 and alias == "Alpha":
                # One signed Core owns three independent policies, including
                # non-adjacent slots. Bravo remains a separate single-slot Core.
                entries = [
                    {"alias": alias, "workdir_path": "/workdirs/01", "policy_revision": digest}
                ]
                for slot, name, agent, repo in (
                    ("03", "AlphaDocs", False, True),
                    ("05", "AlphaAgent", True, False),
                ):
                    target = root / name
                    target.mkdir(mode=0o700)
                    (target / "hello.txt").write_text(name, encoding="utf-8")
                    (target / "task-context").mkdir()
                    if repo:
                        for args in (
                            ["init", "-q", "-b", "docs-fixture"],
                            ["add", "hello.txt"],
                            [
                                "-c",
                                "user.name=Fixture",
                                "-c",
                                "user.email=fixture@example.invalid",
                                "commit",
                                "-qm",
                                "fixture",
                            ],
                        ):
                            subprocess.run(
                                [git, *args],
                                cwd=target,
                                env=git_env,
                                capture_output=True,
                                check=True,
                                timeout=10,
                            )
                    actions = ()
                    if repo and repo_full:
                        remote = root / "docs-fixture-remote.git"
                        for cwd, argv in (
                            (root, ["init", "--bare", "-q", str(remote)]),
                            (target, ["config", "user.name", "Fixture"]),
                            (target, ["config", "user.email", "fixture@example.invalid"]),
                            (target, ["remote", "add", "fixture", str(remote)]),
                            (target, ["config", "branch.docs-fixture.remote", "fixture"]),
                            (
                                target,
                                ["config", "branch.docs-fixture.merge", "refs/heads/docs-fixture"],
                            ),
                        ):
                            subprocess.run(
                                [git, *argv],
                                cwd=cwd,
                                env=git_env,
                                capture_output=True,
                                check=True,
                                timeout=10,
                            )
                        actions = (
                            RepoActionPolicy(
                                name="fixture_check",
                                argv=(git, "rev-parse", "--is-inside-work-tree"),
                                integrity=(
                                    (
                                        "hello.txt",
                                        hashlib.sha256(
                                            (target / "hello.txt").read_bytes()
                                        ).hexdigest(),
                                    ),
                                ),
                            ),
                        )
                    extra_policy = WorkdirAgentPolicy(
                        slot=None,
                        alias=name,
                        host_path=target,
                        mode=AgentMode.WORKSPACE_WRITE if agent else AgentMode.DISABLED,
                        runtimes=frozenset({"codex"}) if agent else frozenset(),
                        read_only=False,
                        repo_ops_enabled=repo,
                        repo_actions=actions,
                    )
                    policies.append(extra_policy)
                    entries.append(
                        {
                            "alias": name,
                            "workdir_path": f"/workdirs/{slot}",
                            "policy_revision": core_workdir_policy_digest(extra_policy),
                        }
                    )
                    extra_workdirs[slot] = (name, target, agent, repo)
                grants[-1] = CoreGrant(
                    core_id,
                    key_id,
                    secret,
                    tuple((e["alias"], e["policy_revision"]) for e in entries),
                    grants[-1].methods
                    | (
                        frozenset({"repo.action", "repo.commit", "repo.push"})
                        if repo_full
                        else frozenset()
                    ),
                )
                _private_json(
                    binding / "core-bridge-binding.json",
                    {
                        "schema_version": 2,
                        "instance_id": core_id,
                        "core_id": core_id,
                        "bridge_id": bridge_id,
                        "key_id": key_id,
                        "secret_hex": secret.hex(),
                        "socket_path": "/run/serverfs-agent-bridge/bridge.sock",
                        "workdirs": entries,
                    },
                )
            port = _port()
            while any(port == core["port"] for core in cores):
                port = _port()
            project = "serverfs-v25-test-" + uuid.uuid4().hex[:12]
            env = {
                **path_env,
                "SERVERFS_IMAGE": IMAGE,
                "SERVERFS_UID": str(os.getuid()),
                "SERVERFS_GID": str(os.getgid()),
                "SERVERFS_HTTP_HOST": "0.0.0.0",
                "SERVERFS_HTTP_PORT": "8000",
                "SERVERFS_HTTP_PUBLISH_HOST": "127.0.0.1",
                "SERVERFS_HTTP_PUBLISH_PORT": str(port),
                "SERVERFS_HTTP_ALLOWED_HOSTS": f"127.0.0.1:{port}",
                "SERVERFS_HTTP_API_KEY": token,
                "SERVERFS_LINUX_INSTANCE_ID": core_id,
                "SERVERFS_LINUX_BRIDGE_ID": bridge_id,
                "SERVERFS_LINUX_CORE_BINDING_DIR": str(binding),
                "SERVERFS_LINUX_BRIDGE_SOCKET_DIR": str(socket_dir),
                "SERVERFS_LINUX_BRIDGE_LOCK_DIR": str(locks),
                "WORKDIR_01_ALIAS": alias,
                "WORKDIR_01_PATH": str(work),
                "WORKDIR_01_READ_ONLY": "false",
                "WORKDIR_01_AGENT_MODE": "workspace-write",
                "WORKDIR_01_AGENT_RUNTIMES": "codex",
                "WORKDIR_01_REPO_OPS_ENABLED": "true",
            }
            for slot in range(2, 17):
                env[f"WORKDIR_{slot:02d}_PATH"] = str(disabled)
            for slot, (name, target, agent, repo) in extra_workdirs.items():
                env.update(
                    {
                        f"WORKDIR_{slot}_ALIAS": name,
                        f"WORKDIR_{slot}_PATH": str(target),
                        f"WORKDIR_{slot}_READ_ONLY": "false",
                        f"WORKDIR_{slot}_AGENT_MODE": "workspace-write" if agent else "disabled",
                        f"WORKDIR_{slot}_AGENT_RUNTIMES": "codex" if agent else "",
                        f"WORKDIR_{slot}_REPO_OPS_ENABLED": "true" if repo else "false",
                    }
                )
            base = [*daemon, "compose", "--env-file", str(env_file), "-p", project]
            for path in files:
                base.extend(("-f", str(path)))
            cores.append(dict(alias=alias, work=work, port=port, token=token, env=env, base=base))

        store = TaskStore(state)
        lease_manager = LeaseManager(
            locks, shared_gid=os.getgid(), lease_ids=[policy.lease_id for policy in policies]
        )
        service = BridgeService(
            store=store,
            policies=PolicyRegistry(policies),
            adapters={"codex": FakeAdapter()},
            lease_manager=lease_manager,
        )

        def make_bridge():
            return BridgeProtocolServer(
                service=service,
                socket_path=socket_dir / "bridge.sock",
                allowed_peer_uid=os.getuid(),
                allowed_peer_gid=os.getgid(),
                v2_only=True,
                core_grant_verifier=CoreGrantVerifier(
                    bridge_id, tuple(grants), nonce_claim=store.claim_core_nonce
                ),
            )

        def run(core, command, *, timeout=45, check=True):
            result = subprocess.run(
                command,
                cwd=root,
                env=core["env"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if check:
                assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
            return result

        def assert_no_source_overlay(core_config, container=None):
            if USE_SOURCE:
                return
            assert "PYTHONPATH" not in core_config.get("environment", {})
            assert all(
                volume.get("target") != "/run/serverfs-candidate-src"
                for volume in core_config.get("volumes", [])
            )
            if container:
                env_result = run(
                    cores[0],
                    [*daemon, "inspect", "--format", "{{json .Config.Env}}", container],
                )
                mount_result = run(
                    cores[0],
                    [*daemon, "inspect", "--format", "{{json .Mounts}}", container],
                )
                assert all(
                    not value.startswith("PYTHONPATH=") for value in json.loads(env_result.stdout)
                )
                assert all(
                    mount.get("Destination") != "/run/serverfs-candidate-src"
                    and "candidate-src" not in mount.get("Source", "")
                    for mount in json.loads(mount_result.stdout)
                )

        async def compose(core, *args, check=True):
            return await asyncio.to_thread(run, core, [*core["base"], *args], check=check)

        async def ready(core):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if await asyncio.to_thread(_http_status, core["port"]) == 401:
                    return
                await asyncio.sleep(0.2)
            logs = await compose(
                core, "logs", "--no-color", "--tail", "30", "serverfs-mcp", check=False
            )
            pytest.fail("HTTP readiness failed: " + (logs.stdout + logs.stderr)[-3000:])

        async def core_identity(core):
            container = (await compose(core, "ps", "-q", "serverfs-mcp")).stdout.strip()
            assert container
            result = await asyncio.to_thread(
                run,
                core,
                [
                    *daemon,
                    "inspect",
                    "--format",
                    "{{.Id}} {{.State.Pid}} {{.State.StartedAt}}",
                    container,
                ],
            )
            return result.stdout.strip()

        async def call(core, name, arguments=None):
            return await _tool(core["port"], core["token"], name, arguments)

        async def read(core):
            result = await call(
                core, "read_text_file", {"workdir": core["alias"], "path": "hello.txt"}
            )
            assert not result.is_error and core["alias"] in str(result.content)

        async def lookup(core, task_id):
            result = await call(core, "get_agent_task", {"task_id": task_id})
            assert not result.is_error and result.structured_content["task_id"] == task_id

        async def submit(core, alias=None):
            result = await call(
                core,
                "submit_agent_task",
                {
                    "runtime": "codex",
                    "workdir": alias or core["alias"],
                    "path": "task-context",
                    "profile": "workspace-write",
                    "prompt": "complete:isolated synthetic smoke",
                },
            )
            assert not result.is_error and result.structured_content, result.content
            return result.structured_content["task_id"]

        async def exercise():
            bridge = make_bridge()
            # Track only projects proven empty before we create resources.
            owned_projects = []
            await asyncio.to_thread(
                run, cores[0], [*daemon, "image", "inspect", "--format", "{{.Id}}", IMAGE]
            )
            try:
                await bridge.start()
                for core in cores:
                    assert not (await compose(core, "ps", "-a", "-q")).stdout.strip()
                    owned_projects.append(core)
                    rendered = await compose(core, "config", "--format", "json")
                    rendered_core = json.loads(rendered.stdout)["services"]["serverfs-mcp"]
                    assert_no_source_overlay(rendered_core)
                    probe = await compose(
                        core,
                        "run",
                        "--no-deps",
                        "--rm",
                        "-T",
                        "--pull",
                        "never",
                        "--entrypoint",
                        "python",
                        "serverfs-mcp",
                        "-c",
                        _PROBE,
                    )
                    print(probe.stdout)
                    assert "signed ping accepted" in probe.stdout
                    await compose(
                        core,
                        "up",
                        "-d",
                        "--no-deps",
                        "--no-build",
                        "--pull",
                        "never",
                        "serverfs-mcp",
                    )
                    container = (await compose(core, "ps", "-q", "serverfs-mcp")).stdout.strip()
                    assert container
                    assert_no_source_overlay(rendered_core, container)
                    published = await compose(core, "port", "serverfs-mcp", "8000", check=False)
                    assert published.returncode == 0 and published.stdout.strip() == (
                        f"127.0.0.1:{core['port']}"
                    ), ("Docker did not publish the requested loopback endpoint", published.stderr)
                    await ready(core)
                    assert await asyncio.to_thread(_http_status, core["port"], "invalid") == 401
                    assert {"submit_agent_task", "inspect_repo"} <= await call(core, "tools/list")
                    await read(core)
                    status = await call(
                        core, "inspect_repo", {"workdir": core["alias"], "view": "status"}
                    )
                    assert not status.is_error, status.content
                    assert core["env"]["SERVERFS_LINUX_INSTANCE_ID"] in str(status.content)

                first = cores[0]
                extra_task = None
                if core_count == 2:
                    for name in ("AlphaDocs", "AlphaAgent"):
                        result = await call(
                            first, "read_text_file", {"workdir": name, "path": "hello.txt"}
                        )
                        assert not result.is_error and name in str(result.content)
                    docs = await call(
                        first, "inspect_repo", {"workdir": "AlphaDocs", "view": "status"}
                    )
                    assert not docs.is_error and "docs-fixture" in str(docs.content)
                    if repo_full:
                        target = root / "AlphaDocs"
                        remote = root / "docs-fixture-remote.git"
                        # Verify the effective push URL in the same isolated HOME; never push
                        # to a configured user/project remote.
                        effective = subprocess.check_output(
                            [git, "remote", "get-url", "--push", "--all", "fixture"],
                            cwd=target,
                            env={**path_env, "XDG_CONFIG_HOME": str(root / "git-config")},
                            text=True,
                            timeout=10,
                        ).splitlines()
                        assert effective == [str(remote)] and remote.is_dir()
                        listing = await call(
                            first, "inspect_repo", {"workdir": "AlphaDocs", "view": "actions"}
                        )
                        assert not listing.is_error
                        assert "fixture_check" in str(listing.structured_content)
                        action = await call(
                            first,
                            "run_repo_action",
                            {"workdir": "AlphaDocs", "action": "fixture_check"},
                        )
                        assert not action.is_error
                        assert action.structured_content["returncode"] == 0
                        assert action.structured_content["stdout"].strip() == "true"
                        changed = await call(
                            first,
                            "create_text_file",
                            {"workdir": "AlphaDocs", "path": "new.txt", "content": "repo-only"},
                        )
                        assert not changed.is_error
                        committed = await call(
                            first,
                            "commit_repo_changes",
                            {"workdir": "AlphaDocs", "paths": ["new.txt"], "message": "fixture"},
                        )
                        assert not committed.is_error
                        commit = committed.structured_content["commit"]
                        pushed = await call(first, "push_repo", {"workdir": "AlphaDocs"})
                        assert not pushed.is_error and pushed.structured_content["pushed"] is True
                        assert pushed.structured_content["commit"] == commit
                        observed = subprocess.check_output(
                            [git, "--git-dir", str(remote), "rev-parse", "refs/heads/docs-fixture"],
                            env=git_env,
                            text=True,
                            timeout=10,
                        ).strip()
                        assert observed == commit
                        denied_repo = await call(
                            cores[1], "inspect_repo", {"workdir": "AlphaDocs", "view": "status"}
                        )
                        assert denied_repo.is_error
                        print("linux_repo_only_four_tools=PASS local_bare_remote=VERIFIED")
                    denied = await call(
                        first, "inspect_repo", {"workdir": "AlphaAgent", "view": "status"}
                    )
                    assert denied.is_error
                    denied = await call(
                        first,
                        "submit_agent_task",
                        {
                            "runtime": "codex",
                            "workdir": "AlphaDocs",
                            "path": "",
                            "profile": "workspace-write",
                            "prompt": "complete:must not run",
                        },
                    )
                    assert denied.is_error
                    submitted_extra_task = await submit(first, "AlphaAgent")
                    del submitted_extra_task
                    extra_page = await call(
                        first,
                        "list_agent_tasks",
                        {"workdir": "AlphaAgent", "status": "all"},
                    )
                    assert not extra_page.is_error, extra_page.content
                    extra_tasks = extra_page.structured_content["tasks"]
                    assert extra_tasks and all(
                        task["workdir"] == "AlphaAgent" for task in extra_tasks
                    )
                    extra_task = extra_tasks[0]["task_id"]
                    alpha_page = await call(
                        first,
                        "list_agent_tasks",
                        {"workdir": "Alpha", "status": "all"},
                    )
                    assert not alpha_page.is_error
                    assert all(
                        task["task_id"] != extra_task
                        for task in alpha_page.structured_content["tasks"]
                    )
                    bravo_page = await call(
                        cores[1],
                        "list_agent_tasks",
                        {"workdir": "Bravo", "status": "all"},
                    )
                    assert not bravo_page.is_error
                    assert all(
                        task["task_id"] != extra_task
                        for task in bravo_page.structured_content["tasks"]
                    )
                    await lookup(first, extra_task)
                    denied = await call(cores[1], "get_agent_task", {"task_id": extra_task})
                    assert denied.is_error and "CORE_TASK_NOT_FOUND" in str(denied.content)
                with lease_manager.acquire_exclusive(alias_lease_id(first["alias"])):
                    denied = await call(
                        first,
                        "create_text_file",
                        {"workdir": first["alias"], "path": "locked.txt", "content": "no"},
                    )
                    assert denied.is_error and "WORKDIR_BUSY" in str(denied.content)
                    assert not (first["work"] / "locked.txt").exists()
                    if core_count == 2:
                        other = cores[1]
                        result = await call(
                            other,
                            "create_text_file",
                            {"workdir": other["alias"], "path": "free.txt", "content": "yes"},
                        )
                        assert not result.is_error, result.content
                        assert (other["work"] / "free.txt").read_text() == "yes"
                written = await call(
                    first,
                    "create_text_file",
                    {"workdir": first["alias"], "path": "written.txt", "content": "ok"},
                )
                assert not written.is_error, written.content
                assert (first["work"] / "written.txt").read_text() == "ok"
                restricted = cores[1] if repo_full else first
                denied_commit = await call(
                    restricted,
                    "commit_repo_changes",
                    {
                        "workdir": restricted["alias"],
                        "paths": ["hello.txt" if repo_full else "written.txt"],
                        "message": "denied",
                    },
                )
                assert denied_commit.is_error and "CORE_NOT_AUTHORIZED" in str(
                    denied_commit.content
                )

                task_ids = []
                for core in cores:
                    submitted_task_id = await submit(core)
                    del submitted_task_id
                    page = await call(
                        core,
                        "list_agent_tasks",
                        {"workdir": core["alias"], "status": "all"},
                    )
                    assert not page.is_error, page.content
                    listed = page.structured_content["tasks"]
                    assert listed and all(task["workdir"] == core["alias"] for task in listed)
                    task_id = listed[0]["task_id"]
                    task_ids.append(task_id)
                    await lookup(core, task_id)
                if core_count == 2:
                    for index, core in enumerate(cores):
                        foreign = cores[1 - index]
                        assert (
                            await asyncio.to_thread(_http_status, core["port"], foreign["token"])
                            == 401
                        )
                        denied = await call(
                            core, "get_agent_task", {"task_id": task_ids[1 - index]}
                        )
                        assert denied.is_error and "CORE_TASK_NOT_FOUND" in str(denied.content)
                        denied = await call(
                            core,
                            "read_text_file",
                            {"workdir": foreign["alias"], "path": "hello.txt"},
                        )
                        assert denied.is_error
                        denied = await call(
                            core, "inspect_repo", {"workdir": foreign["alias"], "view": "status"}
                        )
                        assert denied.is_error

                # Stop/restart just the first container; the other Core and Bridge survive.
                other_identity = await core_identity(cores[1]) if core_count == 2 else None
                before = (socket_dir / "bridge.sock").stat()
                await compose(first, "restart", "--timeout", "3", "serverfs-mcp")
                await ready(first)
                after = (socket_dir / "bridge.sock").stat()
                assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
                await lookup(first, task_ids[0])
                if extra_task is not None:
                    await lookup(first, extra_task)
                if core_count == 2:
                    assert await core_identity(cores[1]) == other_identity
                    await lookup(cores[1], task_ids[1])

                # Stop the Bridge listener; files work, Agent and Repo Ops fail closed.
                identities = [await core_identity(core) for core in cores]
                await bridge.close()
                for core, task_id in zip(cores, task_ids, strict=True):
                    await read(core)
                    assert (await call(core, "get_agent_task", {"task_id": task_id})).is_error
                    assert (
                        await call(
                            core, "inspect_repo", {"workdir": core["alias"], "view": "status"}
                        )
                    ).is_error

                # Core can also restart while the Bridge is absent, retaining the v2 grant.
                await compose(first, "restart", "--timeout", "3", "serverfs-mcp")
                await ready(first)
                await read(first)
                assert (await call(first, "get_agent_task", {"task_id": task_ids[0]})).is_error
                identities[0] = await core_identity(first)

                bridge = make_bridge()
                await bridge.start()
                for core, task_id, identity in zip(cores, task_ids, identities, strict=True):
                    await lookup(core, task_id)
                    assert await core_identity(core) == identity
                if extra_task is not None:
                    await lookup(first, extra_task)
            finally:
                try:
                    # Attempt all owned projects even if cleanup of one fails.
                    failures = []
                    for core in reversed(owned_projects):
                        try:
                            await compose(core, "down", "--timeout", "3", "--remove-orphans")
                            assert not (await compose(core, "ps", "-a", "-q")).stdout.strip()
                        except Exception as exc:
                            failures.append(str(exc))
                    assert not failures, failures
                finally:
                    await bridge.close()

        asyncio.run(exercise())

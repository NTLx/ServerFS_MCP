"""One real isolated macOS named owned Core -> Bridge runtime acceptance.

Explicit --execute only. No launchd registration or production mutation.
The default is provider-free; --real-agent explicitly enables one bounded
Claude text-only task using the host runtime's existing authentication.
Creates a random named instance and removes only its own state.
"""

from __future__ import annotations

import argparse
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
import tempfile
import time
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from serverfs_mcp.darwin_instance_bridge_identity import named_owned_bridge_resources
from serverfs_mcp.darwin_instance_owned_runtime import stage_owned_core_bridge
from serverfs_mcp.darwin_instances import APP_NAME, instance_app_path, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

REPO = Path(__file__).resolve().parents[2]
BRIDGE_PYTHON = REPO / "agent_bridge/.venv/bin/python"
BUILD = REPO / "native/macos/service_manager/build.sh"
PROTECTED = (
    "com.ntlx.serverfs.tunnel",
    "com.ntlx.serverfs.bridge",
    "com.ntlx.serverfs.http-origin",
    "com.ntlx.serverfs.cloudflare",
)


def production_baseline() -> dict[str, str]:
    """Snapshot all current default and named ServerFS jobs, including idle jobs."""
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, check=True, timeout=10
    )
    jobs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2].startswith("com.ntlx.serverfs."):
            pid, _, label = fields
            if pid != "-" and not pid.isdecimal():
                raise RuntimeError("production launchd baseline is invalid")
            jobs[label] = pid
    if not jobs or not any(pid != "-" for pid in jobs.values()):
        raise RuntimeError("production launchd baseline is unavailable")
    return jobs


def shutdown(process: subprocess.Popen | None, *, supervised: bool = False) -> None:
    if process is None:
        return
    if supervised and process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is None and not supervised:
        process.terminate()
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=4)


def start_bridge(
    identity: InstanceIdentity, resource, log: Path, *, via_launcher: bool = False
) -> subprocess.Popen:
    argv = (
        [
            str(instance_app_path(identity) / "Contents/Resources/serverfs-service-launcher"),
            identity.instance_id,
            "agent-bridge",
        ]
        if via_launcher
        else [
            sys.executable,
            "-B",
            "-m",
            "serverfs_mcp.darwin_owned_bridge_supervisor",
            "--instance",
            identity.instance_id,
        ]
    )
    with log.open("wb") as stream:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stream,
            cwd=str(REPO),
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("owned Bridge exited before IPC readiness")
            if resource.socket_path.is_socket():
                item = resource.socket_path.stat()
                if item.st_uid != os.getuid() or stat.S_IMODE(item.st_mode) != 0o600:
                    raise RuntimeError("owned Bridge socket permissions are not 0600")
                return proc
            time.sleep(0.1)
        raise RuntimeError("owned Bridge IPC readiness timed out")
    except BaseException:
        shutdown(proc)
        raise


def start_core(
    identity: InstanceIdentity,
    config: Path,
    root: Path,
    port: int,
    log: Path,
    *,
    via_launcher: bool = False,
    agent_protocol: str = "v1",
) -> subprocess.Popen:
    env = dict(os.environ)
    if via_launcher:
        argv = [
            str(instance_app_path(identity) / "Contents/Resources/serverfs-service-launcher"),
            identity.instance_id,
            "http-origin",
        ]
    else:
        env.update(json.loads((root / "http-origin-environment.json").read_text()))
        argv = [
            sys.executable,
            "-B",
            "-m",
            "serverfs_mcp.cli",
            "serve",
            "--transport",
            "streamable-http",
            "--config",
            str(config),
            "--http-port",
            str(port),
            "--macos-instance",
            identity.instance_id,
        ]
        if agent_protocol == "v2":
            argv.extend(["--macos-agent-protocol", "v2"])
    with log.open("wb") as stream:
        return subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stream,
            cwd=str(REPO),
            start_new_session=True,
        )


def wait_core(proc: subprocess.Popen, port: int) -> None:
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.monotonic() + 15
    with httpx2.Client(trust_env=False, timeout=1) as http:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("owned Core exited before readiness")
            try:
                if http.get(url).status_code == 401:
                    return
            except (httpx2.RequestError, OSError):
                pass
            time.sleep(0.1)
    raise RuntimeError("owned Core loopback HTTP readiness timed out")


async def probe_core(
    port: int, key: str, *, agent_protocol: str = "v1", repo_ops: bool = False
) -> None:
    url = f"http://127.0.0.1:{port}/mcp"
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {key}"},
        timeout=8,
        trust_env=False,
    ) as client:
        async with Client(
            streamable_http_client(url, http_client=client, terminate_on_close=False),
            mode="2026-07-28",
            raise_exceptions=True,
        ) as mcp:
            tools = {tool.name for tool in (await mcp.list_tools()).tools}
            if not {"list_workdirs", "get_agent_task"} <= tools:
                raise RuntimeError("owned Core Agent tools are missing")
            roots = await mcp.call_tool("list_workdirs", {})
            if roots.is_error or roots.structured_content is None:
                raise RuntimeError("owned Core filesystem tool failed")
            expected = {"synthetic", "repo"} if repo_ops else {"synthetic"}
            if {item["alias"] for item in roots.structured_content["workdirs"]} != expected:
                raise RuntimeError("owned Core workdir isolation failed")
            if agent_protocol == "v2":
                if "list_agent_tasks" not in tools:
                    raise RuntimeError("owned v2 Core task discovery tool is missing")
                page = await mcp.call_tool(
                    "list_agent_tasks", {"workdir": "synthetic", "status": "all", "limit": 1}
                )
                if page.is_error or page.structured_content != {
                    "tasks": [],
                    "next_cursor": None,
                }:
                    raise RuntimeError("owned v2 Core task discovery failed")
                foreign = await mcp.call_tool("list_agent_tasks", {"workdir": "foreign"})
                if not foreign.is_error:
                    raise RuntimeError("owned v2 Core accepted an unauthorized alias")
            # Do not submit any Agent task. Missing-task lookup must reach
            # the named Bridge, not another instance or an unavailable socket.
            result = await mcp.call_tool(
                "get_agent_task", {"task_id": "agt_" + secrets.token_hex(12)}
            )
            content = str(result.structured_content or result.content)
            missing_code = (
                "CORE_TASK_NOT_FOUND" if agent_protocol == "v2" else "AGENT_TASK_NOT_FOUND"
            )
            if not result.is_error or missing_code not in content:
                raise RuntimeError("owned Core did not reach its own Bridge task store")


async def recovery_call(
    port: int, key: str, name: str, params: dict, *, expected_error: str | None = None
) -> dict:
    """Each call uses a fresh authenticated MCP client."""
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {key}"}, timeout=15, trust_env=False
    ) as http:
        async with Client(
            streamable_http_client(
                f"http://127.0.0.1:{port}/mcp", http_client=http, terminate_on_close=False
            ),
            mode="2026-07-28",
            raise_exceptions=True,
        ) as client:
            result = await client.call_tool(name, params)
    # Assert after closing the MCP session: an expected tool rejection is
    # not an exception inside the client's async TaskGroup.
    if expected_error is not None:
        if not result.is_error or expected_error + ":" not in str(result.content):
            raise RuntimeError(f"expected rejection missing: {name}/{expected_error}")
        return {}
    if result.is_error:
        raise RuntimeError(f"recovery RPC rejected: {name}")
    if not isinstance(result.structured_content, dict):
        raise RuntimeError(f"recovery RPC invalid result: {name}")
    return result.structured_content


def fixture_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *args],
        env={
            "PATH": os.defpath,
            "HOME": str(root.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file",
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return result.stdout.strip()


def prepare_repo_fixture(base: Path) -> str:
    repo = base / "repo"
    remote = base / "remote.git"
    repo.mkdir(mode=0o700)
    remote.mkdir(mode=0o700)
    fixture_git(remote, "init", "--bare")
    fixture_git(repo, "init", "-b", "fixture")
    fixture_git(repo, "config", "user.name", "ServerFS Fixture")
    fixture_git(repo, "config", "user.email", "fixture@example.invalid")
    fixture_git(repo, "config", "commit.gpgsign", "false")
    readme = repo / "README.md"
    readme.write_text("before\n")
    fixture_git(repo, "add", "README.md")
    fixture_git(repo, "commit", "-m", "fixture baseline")
    fixture_git(repo, "remote", "add", "origin", str(remote))
    fixture_git(repo, "push", "-u", "origin", "fixture")
    action_file = repo / "check.txt"
    action_file.write_text("fixture action\n")
    digest = hashlib.sha256(action_file.read_bytes()).hexdigest()
    return (
        f"[[workdirs]]\nalias='repo'\npath='{repo}'\nread_only=false\n"
        "repo_ops_enabled=true\n"
        "[workdirs.repo_actions.fixture_check]\n"
        "argv=['/bin/cat', 'check.txt']\nmutates=false\nargument='none'\n"
        f"[workdirs.repo_actions.fixture_check.integrity]\n'check.txt'='{digest}'\n"
    )


async def probe_repo_ops(port: int, key: str, base: Path) -> None:
    repo = base / "repo"
    remote = base / "remote.git"
    before = fixture_git(repo, "rev-parse", "HEAD")
    actions = await recovery_call(port, key, "inspect_repo", {"workdir": "repo", "view": "actions"})
    if [item["name"] for item in actions["actions"]] != ["fixture_check"]:
        raise RuntimeError("Repo Ops action catalog mismatch")
    action = await recovery_call(
        port, key, "run_repo_action", {"workdir": "repo", "action": "fixture_check"}
    )
    if action.get("returncode") != 0 or action.get("stdout", "").strip() != "fixture action":
        raise RuntimeError("Repo Ops fixed action failed")
    for name, params, code in (
        ("inspect_repo", {"workdir": "synthetic", "view": "status"}, "REPO_OPS_DISABLED"),
        ("list_agent_tasks", {"workdir": "repo"}, "INVALID_REQUEST"),
        (
            "run_repo_action",
            {"workdir": "repo", "action": "not_allowed"},
            "REPO_ACTION_NOT_ALLOWED",
        ),
    ):
        await recovery_call(port, key, name, params, expected_error=code)
    (repo / "README.md").write_text("after\n")
    status = await recovery_call(port, key, "inspect_repo", {"workdir": "repo", "view": "status"})
    if status["head"] != before or status["unstaged_paths"] != ["README.md"]:
        raise RuntimeError("Repo Ops status mismatch")
    committed = await recovery_call(
        port,
        key,
        "commit_repo_changes",
        {"workdir": "repo", "paths": ["README.md"], "message": "fixture scoped change"},
    )
    head = fixture_git(repo, "rev-parse", "HEAD")
    if committed["commit"] != head or head == before or committed["paths"] != ["README.md"]:
        raise RuntimeError("Repo Ops scoped commit mismatch")
    if fixture_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") != "README.md":
        raise RuntimeError("Repo Ops committed unexpected paths")
    pushed = await recovery_call(port, key, "push_repo", {"workdir": "repo"})
    if pushed != {"pushed": True, "commit": head}:
        raise RuntimeError("Repo Ops push response mismatch")
    if fixture_git(remote, "rev-parse", "refs/heads/fixture") != head:
        raise RuntimeError("Repo Ops local remote ref mismatch")
    if fixture_git(repo, "status", "--porcelain") != "?? check.txt":
        raise RuntimeError("Repo Ops unrelated file was not preserved")


async def submit_text_task(port: int, key: str, marker: str, runtime: str) -> None:
    submitted = await recovery_call(
        port,
        key,
        "submit_agent_task",
        {
            "runtime": runtime,
            "workdir": "synthetic",
            "profile": "workspace-write",
            "prompt": (
                f"Return exactly {marker}. Text only. Do not use tools, read or write files, "
                "run commands, access other systems, or request approvals. Stop after replying."
            ),
        },
    )
    if not isinstance(submitted.get("task_id"), str):
        raise RuntimeError("real task submission returned no handle")
    # Deliberately discard the submission response and task ID.


def task_diagnostic(row: dict, marker: str, events: list[dict] | None = None) -> dict:
    """Build a provider-text-free summary from the public task/event interface."""
    response = row.get("final_response") or row.get("error_message")
    response = response if isinstance(response, str) else ""
    encoded = response.encode("utf-8")
    match = (
        "exact" if response.strip() == marker else "contains" if marker in response else "absent"
    )
    subtype = row.get("provider_completion_subtype")
    if subtype not in (
        "success",
        "error_max_turns",
        "error_during_execution",
        "error_max_budget_usd",
    ):
        subtype = None
    error_code = row.get("error_code")
    if error_code not in (
        None,
        "AGENT_PROVIDER_ERROR",
        "AGENT_RUNTIME_UNAVAILABLE",
        "AGENT_RUNTIME_NOT_READY",
        "AGENT_PROVIDER_DISCONNECTED",
        "AGENT_TASK_TIMEOUT",
    ):
        error_code = "OTHER"
    return {
        "status": row.get("status")
        if row.get("status")
        in {
            "queued",
            "running",
            "waiting_for_approval",
            "waiting_for_question",
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
        }
        else "unknown",
        "error_code": error_code,
        "provider_completion_subtype": subtype,
        "result_bytes": len(encoded),
        "result_sha256": hashlib.sha256(encoded).hexdigest(),
        "marker_match": match,
        "event_types": sorted(
            {
                event.get("event_type")
                for event in (events or [])
                if isinstance(event.get("event_type"), str)
                and event.get("event_type")
                in {
                    "task.started",
                    "task.completed",
                    "task.failed",
                    "task.cancelled",
                    "task.interrupted",
                    "task.result_spooled",
                    "agent.message",
                    "task.interaction.requested",
                }
            }
        ),
    }


async def discover_text_task(
    port: int, key: str, marker: str, diagnostics: list[dict] | None = None
) -> tuple[str, str, str]:
    page = await recovery_call(
        port, key, "list_agent_tasks", {"workdir": "synthetic", "status": "all", "limit": 2}
    )
    tasks = page["tasks"]
    if len(tasks) != 1 or page["next_cursor"] is not None:
        raise RuntimeError("expected one owned task in isolated workdir")
    task_id = tasks[0]["task_id"]
    if tasks[0]["correlation_id"] is not None:
        raise RuntimeError("unexpected correlation metadata")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        row = await recovery_call(port, key, "get_agent_task", {"task_id": task_id})
        status = row["status"]
        events = None
        if status in {"succeeded", "failed", "cancelled", "interrupted"}:
            events = await recovery_call(
                port, key, "read_agent_task_events", {"task_id": task_id, "limit": 100}
            )
            if diagnostics is not None:
                diagnostics.append(task_diagnostic(row, marker, events.get("events", [])))
        if status == "succeeded":
            response = row.get("final_response")
            metadata = row.get("result", {})
            if not isinstance(response, str) or not response.strip():
                raise RuntimeError("real task result empty or invalid")
            encoded = response.encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            if (
                metadata.get("storage") != "inline"
                or metadata.get("size_bytes") != len(encoded)
                or metadata.get("sha256") != digest
                or row.get("final_response_truncated") is not False
            ):
                raise RuntimeError("real task result integrity mismatch")
            if not any(e.get("event_type") == "task.completed" for e in events.get("events", [])):
                raise RuntimeError("real task durable completion event missing")
            # Distinguish provider formatting from a missing sentinel without
            # printing any provider content, identifier or digest.
            if response.strip() == marker:
                match = "exact"
            elif marker in response:
                match = "contains"
            else:
                match = "absent"
            return task_id, digest, match
        if status in {"failed", "cancelled", "interrupted"}:
            error_code = row.get("error_code")
            raise RuntimeError(
                "real task terminal state: "
                + status
                + ", error_code="
                + (error_code if isinstance(error_code, str) else "none")
            )
        if status in {"waiting_for_approval", "waiting_for_question"}:
            break
        await asyncio.sleep(1)
    await recovery_call(port, key, "cancel_agent_task", {"task_id": task_id})
    cancel_deadline = time.monotonic() + 15
    while time.monotonic() < cancel_deadline:
        row = await recovery_call(port, key, "get_agent_task", {"task_id": task_id})
        if row.get("status") in {"succeeded", "failed", "cancelled", "interrupted"}:
            events = await recovery_call(
                port, key, "read_agent_task_events", {"task_id": task_id, "limit": 100}
            )
            if diagnostics is not None:
                diagnostics.append(task_diagnostic(row, marker, events.get("events", [])))
            break
        await asyncio.sleep(1)
    else:
        if diagnostics is not None:
            diagnostics.append({"status": "nonterminal_after_cancel_timeout"})
    raise RuntimeError("real task did not complete without interaction")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--via-launcher",
        action="store_true",
        help="execute each signed native ServiceLauncher without launchd registration",
    )
    parser.add_argument("--agent-protocol", choices=("v1", "v2"), default="v1")
    parser.add_argument("--real-agent", action="store_true")
    parser.add_argument("--runtime", choices=("claude", "qoder"), default="claude")
    parser.add_argument("--repo-ops", action="store_true")
    args = parser.parse_args()
    if args.repo_ops and (args.agent_protocol != "v2" or args.real_agent):
        parser.error("--repo-ops requires v2 and cannot combine with --real-agent")
    if args.real_agent and args.agent_protocol != "v2":
        parser.error("--real-agent requires explicit --agent-protocol v2")
    if not args.execute:
        print("NOT_EXECUTED: explicit --execute required")
        return 2
    via_launcher = args.via_launcher
    baseline = production_baseline()
    instance_id = "v25-oc-" + secrets.token_hex(5)
    identity = InstanceIdentity(instance_id=instance_id, bridge_mode="owned", bridge_id=instance_id)
    root = instance_service_home(identity)
    owner = root.parent
    if owner.exists() or owner.is_symlink():
        raise RuntimeError("synthetic instance owner is occupied")
    core = bridge = None
    reached = False
    recovered = False
    repo_verified = False
    restarted = False
    sentinel_match = None
    task_diagnostics: list[dict] = []
    removed = False
    socket_removed = False
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="serverfs-v25-owned-runtime-") as temp:
        base = Path(temp).resolve()
        try:
            workdir = base / "workdir"
            workdir.mkdir(mode=0o700)
            secrets_dir = base / "secrets"
            secrets_dir.mkdir(mode=0o700)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            key = "synthetic-" + secrets.token_hex(16)
            config = base / "serverfs.toml"
            runtime = args.runtime if args.real_agent else "codex"
            config.write_text(
                f"[agent]\nenabled=true\n[agent.{runtime}]\nenabled=true\n"
                "[[workdirs]]\nalias='synthetic'\n"
                f"path='{workdir}'\nread_only=false\n"
                f"agent_mode='workspace-write'\nagent_runtimes=['{runtime}']\n"
            )
            if args.repo_ops:
                config.write_text(config.read_text() + prepare_repo_fixture(base))
            env_file = secrets_dir / "http.env"
            env_file.write_text(
                f"SERVERFS_HTTP_API_KEY={key}\n"
                f"SERVERFS_HTTP_ALLOWED_HOSTS=synthetic.test,127.0.0.1:{port}\n"
            )
            env_file.chmod(0o600)
            output = base / "build"
            subprocess.run(
                ["bash", str(BUILD), str(output)],
                capture_output=True,
                check=True,
                timeout=120,
            )
            stage_owned_core_bridge(
                identity,
                config,
                env_file,
                port,
                "synthetic.test",
                output / APP_NAME,
                BRIDGE_PYTHON,
                agent_protocol=args.agent_protocol,
            )
            resource = named_owned_bridge_resources(identity)
            bridge = start_bridge(
                identity, resource, base / "bridge.stderr.log", via_launcher=via_launcher
            )
            core = start_core(
                identity,
                config,
                root,
                port,
                base / "core.stderr.log",
                via_launcher=via_launcher,
                agent_protocol=args.agent_protocol,
            )
            wait_core(core, port)
            with httpx2.Client(trust_env=False, timeout=2) as http:
                url = f"http://127.0.0.1:{port}/mcp"
                if (
                    http.get(url).status_code != 401
                    or http.get(url, headers={"Authorization": "Bearer incorrect"}).status_code
                    != 401
                ):
                    raise RuntimeError("owned Core authentication gate failed")
            asyncio.run(
                probe_core(port, key, agent_protocol=args.agent_protocol, repo_ops=args.repo_ops)
            )
            reached = True
            if args.repo_ops:
                asyncio.run(probe_repo_ops(port, key, base))
                repo_verified = True
            if args.real_agent:
                marker = "SERVERFS_RECOVERY_" + secrets.token_hex(12)
                asyncio.run(submit_text_task(port, key, marker, runtime))
                task = asyncio.run(discover_text_task(port, key, marker, task_diagnostics))
                recovered = True
                sentinel_match = task[2]
                bridge_pid = bridge.pid
                shutdown(core)
                core = start_core(
                    identity,
                    config,
                    root,
                    port,
                    base / "core-restarted.stderr.log",
                    via_launcher=via_launcher,
                    agent_protocol=args.agent_protocol,
                )
                wait_core(core, port)
                restarted = (
                    asyncio.run(discover_text_task(port, key, marker, task_diagnostics)) == task
                    and bridge.pid == bridge_pid
                    and bridge.poll() is None
                )
                if not restarted:
                    raise RuntimeError("Core restart recovery failed")
                if any(workdir.iterdir()):
                    raise RuntimeError("text-only fixture workdir was modified")
                if sentinel_match != "exact":
                    raise RuntimeError("real task sentinel mismatch")
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc))
        finally:
            shutdown(core)
            shutdown(bridge)
            resource = named_owned_bridge_resources(identity)
            directory = resource.socket_path.parent
            if directory.is_dir() and not directory.is_symlink():
                try:
                    directory.rmdir()
                except OSError:
                    errors.append("owned Bridge socket directory not empty")
            socket_removed = not directory.exists()
            if owner.is_dir() and not owner.is_symlink():
                try:
                    if owner.parent.name != "instances" or owner.name != instance_id:
                        raise RuntimeError("synthetic owner cleanup identity mismatch")
                    shutil.rmtree(owner)
                except OSError:
                    errors.append("synthetic owner cleanup refused")
            removed = not owner.exists()
            if production_baseline() != baseline:
                errors.append("production PID baseline changed")
    payload = {
        "result": (
            "PASS" if reached and not errors and removed and socket_removed else "NOT_ACCEPTED"
        ),
        "core_to_owned_bridge_rpc": reached,
        "agent_protocol": args.agent_protocol,
        "authenticated_task_discovery": reached if args.agent_protocol == "v2" else None,
        "native_signed_launcher_used": via_launcher,
        "real_agent_recovered": recovered if args.real_agent else None,
        "repo_ops_verified": repo_verified if args.repo_ops else None,
        "core_restart_recovered": restarted if args.real_agent else None,
        "real_agent_sentinel_match": sentinel_match,
        "real_agent_task_diagnostics": task_diagnostics,
        "processes_stopped": all(p is None or p.poll() is not None for p in (core, bridge)),
        "private_owner_removed": removed,
        "socket_namespace_removed": socket_removed,
        "production_pids_unchanged": production_baseline() == baseline,
        "protected_job_count": len(baseline),
        "errors": errors,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

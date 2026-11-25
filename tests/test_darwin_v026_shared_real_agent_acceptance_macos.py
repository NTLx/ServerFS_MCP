"""Opt-in macOS Shared Core SDK-boundary acceptance.

SERVERFS_SHARED_CLAUDE_SYNTHETIC=1 exercises two workdirs without a provider.
SERVERFS_ENABLE_SHARED_CLAUDE_E2E=1 permits a single real diagnostic.
The owner uses a separate Bridge venv and native v2 IPC; the Core is disposable.
No Login Item or provider credential material is inspected.
"""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

SYNTHETIC = os.environ.get("SERVERFS_SHARED_CLAUDE_SYNTHETIC") == "1"
if sys.platform != "darwin" or (
    not SYNTHETIC and os.environ.get("SERVERFS_ENABLE_SHARED_CLAUDE_E2E") != "1"
):
    pytest.skip(
        "requires explicit disposable macOS Shared Core SDK opt-in",
        allow_module_level=True,
    )

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SRC = ROOT / "agent_bridge/src"
if str(BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(BRIDGE_SRC))

from serverfs_agent_bridge.core_grants import core_workdir_policy_digest  # noqa: E402
from serverfs_agent_bridge.models import AgentMode  # noqa: E402
from serverfs_agent_bridge.policy import WorkdirAgentPolicy  # noqa: E402

from serverfs_mcp.core_bridge_client_v2 import (  # noqa: E402
    CoreBridgeClientV2,
    CoreBridgeRemoteError,
)
from serverfs_mcp.darwin_instance_shared_staging import stage_shared_core  # noqa: E402
from serverfs_mcp.darwin_instances import instance_service_home  # noqa: E402
from serverfs_mcp.instance_identity import InstanceIdentity  # noqa: E402
from test_darwin_v0250_shared_native_signed_gate_macos import (
    _create_test_runtime_python,  # noqa: E402
)  # noqa: E402
from test_darwin_v0250_shared_two_core_http_macos import (  # noqa: E402
    _mcp_call,
    _port,
    _private_json,
    _wait_for_core,
)


class _OwnerLoop:
    """Keep Core-side RPC calls on one client event loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def call(self, awaitable, *, timeout: float = 20):
        return asyncio.run_coroutine_threadsafe(awaitable, self.loop).result(timeout=timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("disposable owner Bridge event loop did not stop")
        self.loop.close()


def _start_owner(root: Path) -> subprocess.Popen[bytes]:
    """Run the fixture worker in the existing, independently locked Bridge venv."""
    python = ROOT / "agent_bridge/.venv/bin/python"
    assert python.is_file(), "Bridge venv is required"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONPATH"] = str(BRIDGE_SRC)
    with (root / "owner.stderr").open("wb") as log:
        owner = subprocess.Popen(
            [
                str(python),
                "-B",
                str(ROOT / "tests/e2e/macos_shared_claude_owner.py"),
                "--serve-synthetic" if SYNTHETIC else "--serve",
                str(root),
            ],
            env=env,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
    try:
        deadline = time.monotonic() + 20
        while not (root / "owner-ready").is_file():
            assert owner.poll() is None, "disposable owner exited during startup"
            assert time.monotonic() < deadline, "disposable owner readiness timed out"
            time.sleep(0.1)
        assert (root / "bridge.sock").is_socket()
        return owner
    except BaseException:
        _stop_owner(owner)
        raise


def _stop_owner(owner: subprocess.Popen[bytes] | None) -> None:
    if owner is None:
        return
    if owner.poll() is None:
        owner.terminate()
        try:
            owner.wait(timeout=30)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.wait(timeout=5)
    assert owner.returncode == 0, "disposable owner shutdown not proven"


def _read_terminal_metadata(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, list) and len(value) == 2
    assert set(value[0]) == {"assistant_error"} and type(value[0]["assistant_error"]) is bool
    assert set(value[1]) == {
        "result_is_error",
        "result_subtype",
        "selected_source",
        "selected",
        "assistant_text",
    }
    assert value[1]["selected_source"] in {"result", "assistant_text"}
    for key in ("selected", "assistant_text"):
        entry = value[1][key]
        assert set(entry) == {"size_bytes", "sha256"}
        assert type(entry["size_bytes"]) is int and entry["size_bytes"] >= 0
        assert isinstance(entry["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
    assert type(value[1]["result_is_error"]) is bool
    assert value[1]["result_subtype"] in {
        "success",
        "error_max_turns",
        "error_during_execution",
        "error_max_budget_usd",
        "other",
    }
    return value


def _launchd_baseline() -> dict[str, str]:
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, timeout=10, check=True
    )
    return {
        fields[2]: fields[0]
        for line in result.stdout.splitlines()
        if len(fields := line.split()) == 3 and fields[2].startswith("com.ntlx.serverfs.")
    }


def _stop(child: subprocess.Popen[bytes] | None) -> None:
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=6)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


async def _call(port: int, token: str, tool: str, args: dict) -> dict:
    response = await _mcp_call(port, token, tool, args)
    if response.is_error or response.structured_content is None:
        raise RuntimeError(f"MCP tool failed: {tool}")
    return response.structured_content


async def _await_task(
    port: int,
    token: str,
    task_id: str,
    marker: str,
    diagnostics: list[dict],
    metadata_path: Path,
) -> dict:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        row = await _call(port, token, "get_agent_task", {"task_id": task_id})
        state = row.get("status")
        if state in {"succeeded", "failed", "cancelled", "interrupted"}:
            events = await _call(
                port, token, "read_agent_task_events", {"task_id": task_id, "limit": 100}
            )
            response = row.get("final_response") or row.get("error_message") or ""
            encoded = response.encode("utf-8") if isinstance(response, str) else b""
            safe = {
                "status": state,
                "error_code": row.get("error_code"),
                "classification": (
                    "PROVIDER_AUTH_OR_RUNTIME_FAILURE"
                    if row.get("error_code") == "AGENT_PROVIDER_ERROR"
                    else "TASK_TERMINAL_FAILURE"
                )
                if state != "succeeded"
                else ("COMPLETED" if response.strip() == marker else "PROVIDER_RESPONSE_MISMATCH"),
                "result_bytes": len(encoded),
                "result_sha256": hashlib.sha256(encoded).hexdigest(),
                "marker_match": (
                    "exact"
                    if response.strip() == marker
                    else "contains"
                    if marker in response
                    else "absent"
                ),
                "sdk_terminal": _read_terminal_metadata(metadata_path),
                "event_types": sorted(
                    e.get("event_type")
                    for e in events.get("events", [])
                    if e.get("event_type")
                    in {
                        "task.started",
                        "task.completed",
                        "task.failed",
                        "task.cancelled",
                        "task.interrupted",
                        "task.interaction.requested",
                    }
                ),
            }
            metadata = safe["sdk_terminal"]
            if state == "succeeded":
                persisted = row.get("result") or {}
                inline_verified = (
                    persisted.get("storage") == "inline"
                    and row.get("final_response_truncated") is False
                    and persisted.get("size_bytes") == safe["result_bytes"]
                    and persisted.get("sha256") == safe["result_sha256"]
                )
                safe["inline_integrity"] = inline_verified
                safe["sdk_to_public"] = "UNVERIFIED"
                if inline_verified and metadata:
                    selected = metadata[1]["selected"]
                    safe["sdk_to_public"] = (
                        "MATCH"
                        if selected["size_bytes"] == safe["result_bytes"]
                        and selected["sha256"] == safe["result_sha256"]
                        else "DIFFERENT_POSSIBLE_REDACTION"
                    )
            diagnostics.append(safe)
            return safe
        if state in {"waiting_for_approval", "waiting_for_question"}:
            diagnostics.append(
                {
                    "status": state,
                    "classification": "HUMAN_APPROVAL_REQUIRED"
                    if state == "waiting_for_approval"
                    else "HUMAN_INPUT_REQUIRED",
                }
            )
            pytest.fail("Claude requested a real interaction/approval; acceptance stopped")
        await asyncio.sleep(1)
    pytest.fail("Claude task did not reach a terminal state within 120 seconds")


def _evidence_path(tmp_path: Path) -> Path:
    # Ordinary repeatable fixture runs use pytest-owned scratch. A maintainer
    # may explicitly select a fresh report name under the diagnostic directory.
    name = os.environ.get("SERVERFS_SHARED_CLAUDE_EVIDENCE")
    if name is None:
        return tmp_path / "shared-claude-evidence.json"
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*[.]json", name)
    return ROOT / "docs/diagnostics" / name


def _save_evidence(path: Path, record: dict, *, initial: bool = False) -> None:
    """Persist only the fixture's allowlisted evidence, never provider output."""
    if initial:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write("\n")
        return
    pending = path.with_suffix(".pending")
    with pending.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, sort_keys=True)
        stream.write("\n")
    pending.replace(path)


def _prompt(marker: str) -> str:
    return (
        f"Return exactly {marker}. Text only. Do not use tools, read or "
        "write files, run commands, access other systems, or request "
        "approvals. Stop after replying."
    )


def test_shared_core_real_claude_single_task_diagnostic(tmp_path: Path) -> None:
    evidence_path = _evidence_path(tmp_path)
    _save_evidence(evidence_path, {"phase": "started", "result": "UNVERIFIED"}, initial=True)
    baseline = _launchd_baseline()
    suffix = secrets.token_hex(5)
    bridge_id = f"shclaude-{suffix}"
    identity = InstanceIdentity(f"shcore-{suffix}", "shared", bridge_id)
    secret = secrets.token_bytes(32)
    core_token = "synthetic-" + secrets.token_hex(24)
    owner: subprocess.Popen[bytes] | None = None
    owner_loop: _OwnerLoop | None = None
    child: subprocess.Popen[bytes] | None = None
    temporary: tempfile.TemporaryDirectory[str] | None = None
    diagnostics: list[dict] = []
    outcome = "NOT_ACCEPTED"
    owner_survived_core_restart = False
    temporary = tempfile.TemporaryDirectory(prefix="sf-v026-shared-claude-", dir="/private/tmp")
    root = Path(temporary.name).resolve(strict=True)
    try:
        root.chmod(0o700)
        home = root / "home"
        serverfs_home = home / "Library/Application Support/ServerFS"
        (serverfs_home / "instances").mkdir(parents=True, mode=0o700)
        for directory in (
            home,
            home / "Library",
            home / "Library/Application Support",
            serverfs_home,
            serverfs_home / "instances",
        ):
            directory.chmod(0o700)
        aliases = ("First", "Second")
        markers = {alias: "SF_SHARED_" + secrets.token_hex(12) for alias in aliases}
        expected = {}
        if SYNTHETIC:
            for alias, marker in markers.items():
                encoded = _prompt(marker).encode("utf-8")
                expected[alias] = {
                    "marker": marker,
                    "prompt": {
                        "size_bytes": len(encoded),
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                    },
                }
        paths = {alias: root / alias for alias in aliases}
        for path in paths.values():
            path.mkdir(mode=0o700)
        socket_path = root / "bridge.sock"
        bridge_config = root / "bridge.json"
        _private_json(
            bridge_config,
            {"protocol_mode": "v2", "bridge_id": bridge_id, "socket_path": str(socket_path)},
        )
        policies = [
            WorkdirAgentPolicy(
                slot=None,
                alias=alias,
                host_path=paths[alias],
                mode=AgentMode.WORKSPACE_WRITE,
                runtimes=frozenset({"claude"}),
                read_only=False,
            )
            for alias in aliases
        ]
        digests = {p.alias: core_workdir_policy_digest(p) for p in policies}
        foreign_id = "foreign-" + suffix
        foreign_secret = secrets.token_bytes(32)
        _private_json(
            root / "owner-input.json",
            {
                "aliases": aliases,
                "synthetic": SYNTHETIC,
                "expected": expected,
                "digests": digests,
                "bridge_id": bridge_id,
                "core_id": identity.instance_id,
                "key_id": "key-" + suffix,
                "secret_hex": secret.hex(),
                "foreign_id": foreign_id,
                "foreign_key_id": "key-foreign-" + suffix,
                "foreign_secret_hex": foreign_secret.hex(),
            },
        )
        owner = _start_owner(root)
        owner_loop = _OwnerLoop()

        port = _port()
        config = root / "core.toml"
        config.write_text(
            "[agent]\nenabled=true\n[agent.claude]\nenabled=true\n"
            + "".join(
                "[[workdirs]]\n"
                + f"alias='{alias}'\npath='{paths[alias]}'\n"
                + "read_only=false\nagent_mode='workspace-write'\n"
                + "agent_runtimes=['claude']\n"
                for alias in aliases
            ),
            encoding="utf-8",
        )
        config.chmod(0o600)
        env_file = root / "http.env"
        env_file.write_text(
            f"SERVERFS_HTTP_API_KEY={core_token}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=core.invalid,127.0.0.1:{port}\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        binding = root / "binding.json"
        _private_json(
            binding,
            {
                "schema_version": 2,
                "bridge_id": bridge_id,
                "core_id": identity.instance_id,
                "key_id": "key-" + suffix,
                "secret_hex": secret.hex(),
                "bridge_config_path": str(bridge_config),
                "socket_path": str(socket_path),
                "workdirs": [
                    {"alias": a, "path": str(paths[a]), "policy_revision": digests[a]}
                    for a in aliases
                ],
            },
        )
        runtime = _create_test_runtime_python(root, ROOT)
        build = subprocess.run(
            ["bash", str(ROOT / "native/macos/service_manager/build.sh"), str(root / "build")],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert build.returncode == 0, "private signed service template build failed"
        stage_shared_core(
            identity,
            config,
            env_file,
            binding,
            port,
            "core.invalid",
            root / "build/ServerFSServiceManager.app",
            runtime,
            home=home,
        )
        service_home = instance_service_home(identity, home=home)
        launch_env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON"))
        }
        launch_env.update(
            {
                # Core state stays in scratch; the separate owner retains normal HOME.
                "HOME": str(home),
                "SERVERFS_MACOS_INSTANCE_ID": identity.instance_id,
                "SERVERFS_HTTP_API_KEY": core_token,
                "SERVERFS_HTTP_ALLOWED_HOSTS": f"core.invalid,127.0.0.1:{port}",
                "SERVERFS_DATA_HOME": str(service_home / "data"),
                "SERVERFS_FILE_INGRESS_ENABLED": "false",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        argv = [
            str(runtime),
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
            "--macos-shared-bridge",
            bridge_id,
        ]
        log_path = root / "core.stderr.log"
        with log_path.open("wb") as log:
            child = subprocess.Popen(
                argv,
                env=launch_env,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                start_new_session=True,
            )
            owner_loop.call(_wait_for_core(child, port, log_path))

            names = owner_loop.call(_mcp_call(port, core_token, "list-tools", {}))
            assert {"submit_agent_task", "list_agent_tasks", "get_agent_task"} <= names
            assert (
                not {
                    "inspect_repo",
                    "run_repo_action",
                    "commit_repo_changes",
                    "push_repo",
                }
                & names
            )
            roots = owner_loop.call(_call(port, core_token, "list_workdirs", {}))
            assert {w["alias"] for w in roots["workdirs"]} == set(aliases)
            denied = owner_loop.call(
                _mcp_call(port, core_token, "list_agent_tasks", {"workdir": "Unlisted"})
            )
            assert denied.is_error

            observed = {}
            for alias in aliases if SYNTHETIC else aliases[:1]:
                marker = markers[alias]
                metadata_path = root / "metadata.json"
                metadata_path.unlink(missing_ok=True)
                submitted = owner_loop.call(
                    _call(
                        port,
                        core_token,
                        "submit_agent_task",
                        {
                            "runtime": "claude",
                            "workdir": alias,
                            "profile": "workspace-write",
                            "prompt": _prompt(marker),
                        },
                    )
                )
                task_id = submitted["task_id"]
                page = owner_loop.call(
                    _call(
                        port,
                        core_token,
                        "list_agent_tasks",
                        {"workdir": alias, "status": "all", "limit": 10},
                    )
                )
                assert [t["task_id"] for t in page["tasks"]] == [task_id]
                assert all(t["workdir"] == alias for t in page["tasks"])
                task_id = page["tasks"][0]["task_id"]
                terminal = owner_loop.call(
                    _await_task(port, core_token, task_id, marker, diagnostics, metadata_path),
                    timeout=140,
                )
                terminal["workdir"] = alias
                _save_evidence(
                    evidence_path,
                    {
                        "phase": "terminal",
                        "result": "UNVERIFIED",
                        "tasks": diagnostics,
                    },
                )
                metadata = terminal.get("sdk_terminal") or []
                successful = (
                    terminal.get("status") == "succeeded"
                    and terminal.get("marker_match") == "exact"
                    and terminal.get("inline_integrity") is True
                    and terminal.get("sdk_to_public") == "MATCH"
                    and len(metadata) == 2
                    and metadata[0]["assistant_error"] is False
                    and metadata[1]["result_is_error"] is False
                    and metadata[1]["result_subtype"] == "success"
                )
                if successful:
                    observed[alias] = (task_id, terminal["result_sha256"])
                if SYNTHETIC:
                    assert successful, "Synthetic SDK task did not satisfy the contract"
            successful_diagnostic = len(observed) == (2 if SYNTHETIC else 1)
            foreign_client = CoreBridgeClientV2(
                socket_path,
                bridge_id=bridge_id,
                core_id=foreign_id,
                key_id="key-foreign-" + suffix,
                secret=foreign_secret,
                workdir_alias=aliases[0],
                policy_revision=digests[aliases[0]],
                timeout_seconds=5,
            )
            foreign_target = observed.get(aliases[0], (task_id, ""))[0]
            try:
                owner_loop.call(foreign_client.call("task.get", {"task_id": foreign_target}))
            except CoreBridgeRemoteError as exc:
                assert exc.code == "CORE_TASK_NOT_FOUND"
            else:
                pytest.fail("foreign Core read a Shared Core task")
            assert owner is not None and owner.poll() is None and socket_path.is_socket()
            _stop(child)
            child = subprocess.Popen(
                argv,
                env=launch_env,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                start_new_session=True,
            )
            owner_loop.call(_wait_for_core(child, port, log_path))
            assert socket_path.is_socket() and owner is not None and owner.poll() is None
            owner_survived_core_restart = True
            if successful_diagnostic:
                for alias, (task_id, digest) in observed.items():
                    page = owner_loop.call(
                        _call(
                            port,
                            core_token,
                            "list_agent_tasks",
                            {"workdir": alias, "status": "all", "limit": 10},
                        )
                    )
                    assert [t["task_id"] for t in page["tasks"]] == [task_id]
                    recovered = owner_loop.call(
                        _call(port, core_token, "get_agent_task", {"task_id": task_id})
                    )
                    assert recovered["status"] == "succeeded"
                    assert recovered["task_id"] == task_id
                    recovered_hash = hashlib.sha256(
                        recovered["final_response"].encode("utf-8")
                    ).hexdigest()
                    assert recovered_hash == digest
                outcome = "SYNTHETIC_SDK_PASS" if SYNTHETIC else "DIAGNOSTIC_ONLY"
            _stop(child)
            child = None
        _stop_owner(owner)
        owner = None
        owner_loop.stop()
        owner_loop = None
        assert not socket_path.exists()
    finally:
        if sys.exc_info()[0] is not None:
            outcome = "NOT_ACCEPTED"
        _stop(child)
        try:
            _stop_owner(owner)
        except AssertionError:
            outcome = "NOT_ACCEPTED"
            diagnostics.append({"classification": "OWNER_CLEANUP_UNPROVEN"})
        if owner_loop is not None:
            owner_loop.stop()
        baseline_unchanged = _launchd_baseline() == baseline
        if not baseline_unchanged:
            outcome = "NOT_ACCEPTED"
            diagnostics.append({"classification": "PRODUCTION_BASELINE_CHANGED"})
        if temporary is not None:
            temporary.cleanup()
        cleaned = not root.exists()
        if not cleaned:
            outcome = "NOT_ACCEPTED"
        report = {
            "phase": "finished",
            "result": outcome,
            "runtime": "claude",
            "workdirs": 2,
            "tasks": diagnostics,
            "verified_workdirs": (
                2 if outcome == "SYNTHETIC_SDK_PASS" else int(outcome == "DIAGNOSTIC_ONLY")
            ),
            "real_provider": not SYNTHETIC,
            "login_items_registered": False,
            "repo_ops_enabled": False,
            "owner_survived_core_restart": owner_survived_core_restart,
            "production_baseline_unchanged": baseline_unchanged,
            "temporary_directory_cleaned": cleaned,
        }
        _save_evidence(evidence_path, report)
        print(json.dumps(report, sort_keys=True), flush=True)
    assert outcome == ("SYNTHETIC_SDK_PASS" if SYNTHETIC else "DIAGNOSTIC_ONLY")

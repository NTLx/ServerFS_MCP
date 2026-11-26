"""Opt-in Linux Docker L1 proof for one signed Core with two real Claude aliases."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

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
ACCEPT = os.environ.get("SERVERFS_LINUX_REAL_AGENT_ACCEPT", "")


def _result_evidence(result: object, task: dict, marker: str) -> dict:
    """Separate semantic evidence from transport integrity without recording provider text."""
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        structured = {}
    text = structured.get("text")
    valid_text = isinstance(text, str)
    raw = text.encode("utf-8") if valid_text else b""
    metadata = task.get("result")
    if not isinstance(metadata, dict):
        metadata = {}
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "tool_ok": not getattr(result, "is_error", True),
        "task_succeeded": task.get("status") == "succeeded",
        "text_present": valid_text,
        "result_bytes": len(raw),
        "result_sha256": digest,
        "marker_exact": valid_text and text.strip() == marker,
        "marker_present": valid_text and marker in text,
        "offset_ok": structured.get("offset_bytes") == 0,
        "next_offset_ok": structured.get("next_offset_bytes") == len(raw),
        "eof": structured.get("eof") is True,
        "spooled": metadata.get("storage") == "spool",
        "size_match": metadata.get("size_bytes") == len(raw),
        "sha256_match": metadata.get("sha256") == digest,
    }


def _result_matches_marker(result: object, task: dict, marker: str) -> bool:
    evidence = _result_evidence(result, task, marker)
    return all(
        evidence[key]
        for key in (
            "tool_ok",
            "task_succeeded",
            "text_present",
            "marker_exact",
            "offset_ok",
            "next_offset_ok",
            "eof",
            "spooled",
            "size_match",
            "sha256_match",
        )
    )


@pytest.mark.parametrize(
    ("text", "eof", "valid_hash", "is_error", "expected"),
    [
        ("marker", True, True, False, True),
        ("marker\n", True, True, False, True),
        ("prefix marker", True, True, False, False),
        ("marker", False, True, False, False),
        ("marker", True, False, False, False),
        ("marker", True, True, True, False),
        ("", True, True, False, False),
    ],
)
def test_synthetic_result_marker_shapes(text, eof, valid_hash, is_error, expected) -> None:
    raw = text.encode("utf-8")
    result = SimpleNamespace(
        is_error=is_error,
        structured_content={
            "text": text,
            "offset_bytes": 0,
            "next_offset_bytes": len(raw),
            "eof": eof,
        },
    )
    task = {
        "status": "succeeded",
        "final_response": "marker",
        "result": {
            "storage": "spool",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest() if valid_hash else "0" * 64,
        },
    }
    assert _result_matches_marker(result, task, "marker") is expected


def test_spool_integrity_distinct_from_semantic_failure() -> None:
    text = "synthetic explanation marker"
    raw = text.encode("utf-8")
    result = SimpleNamespace(
        is_error=False,
        structured_content={
            "text": text,
            "offset_bytes": 0,
            "next_offset_bytes": len(raw),
            "eof": True,
        },
    )
    task = {
        "status": "succeeded",
        "result": {
            "storage": "spool",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    }
    evidence = _result_evidence(result, task, "marker")
    assert evidence["marker_present"] and not evidence["marker_exact"]
    assert evidence["sha256_match"] and evidence["size_match"] and evidence["eof"]
    assert not _result_matches_marker(result, task, "marker")
    assert text not in json.dumps(evidence)


@pytest.mark.skipif(
    sys.platform != "linux" or not IMAGE or ACCEPT != "claude",
    reason="requires Linux, explicit Docker image and SERVERFS_LINUX_REAL_AGENT_ACCEPT=claude",
)
def test_one_signed_core_two_real_claude_aliases(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    assert os.getuid() != 0
    docker, claude = (shutil.which(name) for name in ("docker", "claude"))
    bridge_python = Path(
        os.environ.get("SERVERFS_LINUX_BRIDGE_PYTHON", str(ROOT / "agent_bridge/.venv/bin/python"))
    )
    assert docker and claude and bridge_python.is_absolute() and bridge_python.is_file()
    daemon = [docker, "--host", "unix:///var/run/docker.sock"]
    project = "serverfs-v026-l1-" + uuid.uuid4().hex[:12]
    # Keep the Unix socket path short without using a shared fixed directory.
    root = Path(tempfile.mkdtemp(prefix="s-", dir=tmp_path.parent)).resolve()
    root.chmod(0o700)
    cleanup_allowed = False

    def cleanup_fixture() -> None:
        if not cleanup_allowed:
            print("linux_l1_fixture_cleanup=RETAINED", flush=True)
            return
        # The isolated fixture must not contain links or special files at removal time.
        # Never follow a provider-created link or inspect unexpected file contents.
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in (*dirs, *files):
                info = (Path(directory) / name).lstat()
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    print("linux_l1_fixture_cleanup=RETAINED_UNEXPECTED_TYPE", flush=True)
                    pytest.fail("fixture contains unexpected file type; retained")
        shutil.rmtree(root)
        assert not root.exists()
        print("linux_l1_fixture_cleanup=PASS", flush=True)

    request.addfinalizer(cleanup_fixture)
    bridge_root = root / "bridge"
    socket_dir, state, locks = (bridge_root / part for part in ("run", "state", "locks"))
    assert len(os.fsencode(socket_dir / "bridge.sock")) <= 107, (
        "fixture Unix socket path exceeds Linux limit; use a shorter --basetemp"
    )
    binding_dir, disabled = root / "binding", root / "disabled"
    workdirs = {"slot01": root / "work01", "slot03": root / "work03"}
    for directory in (socket_dir, state, locks, binding_dir, disabled, *workdirs.values()):
        directory.mkdir(mode=0o700, parents=True)
    (disabled / ".serverfs-disabled").touch()
    markers = {alias: "SERVERFS_L1_" + uuid.uuid4().hex for alias in ("Repo", "Docs")}
    for alias, directory in zip(markers, workdirs.values(), strict=True):
        (directory / "marker.txt").write_text(markers[alias] + "\n", encoding="utf-8")
    before = {
        alias: hashlib.sha256((directory / "marker.txt").read_bytes()).hexdigest()
        for alias, directory in zip(markers, workdirs.values(), strict=True)
    }

    def fixture_state() -> dict:
        evidence = {}
        root_ok = stat.S_ISDIR(root.lstat().st_mode)
        for alias, directory in zip(markers, workdirs.values(), strict=True):
            try:
                directory_ok = root_ok and stat.S_ISDIR(directory.lstat().st_mode)
                names = {path.name for path in directory.iterdir()} if directory_ok else set()
                marker = directory / "marker.txt"
                info = marker.lstat() if directory_ok and "marker.txt" in names else None
                regular = info is not None and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                evidence[alias] = {
                    "entries_unchanged": names == {"marker.txt"},
                    "marker_unchanged": regular
                    and (hashlib.sha256(marker.read_bytes()).hexdigest() == before[alias]),
                }
            except OSError:
                evidence[alias] = {"entries_unchanged": False, "marker_unchanged": False}
        return evidence

    api_key, core_id = "synthetic-" + uuid.uuid4().hex, "core-l1-" + uuid.uuid4().hex[:8]
    bridge_id, key_id = "bridge-l1-" + uuid.uuid4().hex[:8], "key-l1-" + uuid.uuid4().hex[:8]
    secret, port = os.urandom(32), _port()
    config_path = root / "bridge-config.json"
    config = {
        "protocol_mode": "v2",
        "bridge_id": bridge_id,
        "lease_key": "alias",
        "socket_path": str(socket_dir / "bridge.sock"),
        "state_dir": str(state),
        "lock_dir": str(locks),
        "allowed_peer_uid": os.getuid(),
        "allowed_peer_gid": os.getgid(),
        "enable_fake_runtime": False,
        "limits": {"result_spool_threshold_bytes": 1},
        "claude": {"enabled": True, "claude_bin": claude, "use_proxy": False},
        "workdirs": [
            {
                "slot": None,
                "alias": alias,
                "host_path": str(path),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": ["claude"],
                "repo_ops_enabled": False,
            }
            for alias, path in zip(markers, workdirs.values(), strict=True)
        ],
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config_path.chmod(0o600)
    bridge_config = BridgeConfig.load(config_path)
    policies = bridge_config.policies
    grant = {
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
                "workdirs": [
                    {
                        "alias": alias,
                        "policy_sha256": core_workdir_policy_digest(policies.get(alias)),
                    }
                    for alias in markers
                ],
            }
        ],
    }
    provision_private_core_grants(
        store=TaskStore(state), bridge_id=bridge_id, policies=policies, document=grant
    )
    binding = {
        "schema_version": 2,
        "instance_id": core_id,
        "core_id": core_id,
        "bridge_id": bridge_id,
        "key_id": key_id,
        "secret_hex": secret.hex(),
        "workdirs": [
            {
                "alias": alias,
                "workdir_path": f"/workdirs/{slot}",
                "policy_revision": core_workdir_policy_digest(policies.get(alias)),
            }
            for alias, slot in (("Repo", "01"), ("Docs", "03"))
        ],
        "socket_path": "/run/serverfs-agent-bridge/bridge.sock",
    }
    (binding_dir / "core-bridge-binding.json").write_text(json.dumps(binding), encoding="utf-8")
    (binding_dir / "core-bridge-binding.json").chmod(0o400)
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
    }
    for slot in range(1, 17):
        suffix = f"{slot:02d}"
        env[f"WORKDIR_{suffix}_PATH"] = str(
            workdirs["slot01"] if slot == 1 else workdirs["slot03"] if slot == 3 else disabled
        )
        if slot in (1, 3):
            alias = "Repo" if slot == 1 else "Docs"
            env.update(
                {
                    f"WORKDIR_{suffix}_ALIAS": alias,
                    f"WORKDIR_{suffix}_READ_ONLY": "false",
                    f"WORKDIR_{suffix}_AGENT_MODE": "workspace-write",
                    f"WORKDIR_{suffix}_AGENT_RUNTIMES": "claude",
                    f"WORKDIR_{suffix}_REPO_OPS_ENABLED": "false",
                }
            )
    compose_files = [
        ROOT / name
        for name in (
            "compose.yml",
            "compose.http.yml",
            "deployment/linux/compose.publish-http.yml",
            "deployment/linux/compose.bridge-v2.yml",
        )
    ]
    base = [*daemon, "compose", "--env-file", str(empty_env), "-p", project]
    for path in compose_files:
        base.extend(("-f", str(path)))
    bridge_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SERVERFS_", "PYTHON"))
    }
    bridge_env.update(PATH=os.environ.get("PATH", os.defpath), HOME=str(Path.home()))
    launcher = (
        "import runpy,sys;from pathlib import Path;"
        "source=Path(sys.argv.pop(1)).resolve();sys.path.insert(0,str(source));"
        "import serverfs_agent_bridge as package;"
        "assert Path(package.__file__).resolve().is_relative_to(source);"
        "runpy.run_module('serverfs_agent_bridge.main',run_name='__main__')"
    )
    bridge_command = [
        str(bridge_python),
        "-I",
        "-B",
        "-c",
        launcher,
        str(BRIDGE_SRC),
        "--config",
        str(config_path),
    ]
    bridge: subprocess.Popen[bytes] | None = None
    submitted_ids: list[str] = []

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
            assert result.returncode == 0, (result.stdout + result.stderr)[-2000:]
        return result

    def call(name: str, arguments: dict | None = None):
        return asyncio.run(_tool(port, api_key, name, arguments))

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
        while time.monotonic() < deadline and not socket_path.is_socket():
            assert bridge.poll() is None, "temporary Bridge exited during startup"
            time.sleep(0.1)
        assert socket_path.is_socket(), "temporary Bridge socket did not appear"
        compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "serverfs-mcp")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _http_status(port) != 401:
            time.sleep(0.2)
        assert _http_status(port) == 401, "Docker Core did not become ready"
        denied = call(
            "submit_agent_task",
            {
                "runtime": "claude",
                "workdir": "Foreign",
                "path": "",
                "profile": "workspace-write",
                "prompt": "read marker",
            },
        )
        assert denied.is_error, "unsigned Foreign alias unexpectedly accepted"
        # Submit sequentially so a failed first task cannot leave a second unobserved task.
        for alias in markers:
            submitted = call(
                "submit_agent_task",
                {
                    "runtime": "claude",
                    "workdir": alias,
                    "path": "",
                    "profile": "workspace-write",
                    "prompt": (
                        "Read only marker.txt in the current directory. Do not modify any file, "
                        "do not use network or other tools, and do not access anything outside "
                        "this workdir. Return exactly the single marker text found in the file, "
                        "with no explanation."
                    ),
                },
            )
            assert not submitted.is_error, f"Claude submission denied for authorized alias {alias}"
            task_id = (submitted.structured_content or {}).get("task_id")
            assert isinstance(task_id, str) and task_id
            submitted_ids.append(task_id)
            task = None
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                page = call("list_agent_tasks", {"workdir": alias, "status": "all"})
                matches = [
                    item
                    for item in (page.structured_content or {}).get("tasks", [])
                    if item.get("workdir") == alias
                ]
                if matches:
                    assert len(matches) == 1
                    assert matches[0]["task_id"] == task_id
                    lookup = call("get_agent_task", {"task_id": matches[0]["task_id"]})
                    assert not lookup.is_error
                    task = lookup.structured_content or {}
                    if task.get("status") in {"waiting_for_approval", "waiting_for_question"}:
                        call("cancel_agent_task", {"task_id": matches[0]["task_id"]})
                        pytest.fail("provider required interaction; no approval granted")
                    if task.get("status") in {"succeeded", "failed", "cancelled", "interrupted"}:
                        break
                time.sleep(1)
            assert task and task.get("status") == "succeeded", (
                f"Claude task failed for alias {alias}"
            )
            result = call(
                "read_agent_task_result",
                {"task_id": task["task_id"], "offset_bytes": 0, "max_bytes": 4096},
            )
            result_evidence = _result_evidence(result, task, markers[alias])
            fixture_evidence = fixture_state()
            print(
                "linux_l1_result_evidence="
                + json.dumps(
                    {
                        "alias": alias,
                        "result": result_evidence,
                        "fixtures": fixture_evidence,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            assert _result_matches_marker(result, task, markers[alias]), (
                f"exact spooled marker or digest proof failed for alias {alias}"
            )
            assert all(all(item.values()) for item in fixture_evidence.values()), (
                "fixture entry or marker integrity failure"
            )
        print("linux_l1_multi_alias=PASS aliases=2 foreign_alias=DENIED marker_reads=2")
    finally:
        unverified = 0
        terminal = {"succeeded", "failed", "cancelled", "interrupted"}
        for task_id in submitted_ids:
            confirmed = False
            try:
                row = call("get_agent_task", {"task_id": task_id})
                confirmed = (
                    not row.is_error and (row.structured_content or {}).get("status") in terminal
                )
                if not confirmed:
                    call("cancel_agent_task", {"task_id": task_id})
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        row = call("get_agent_task", {"task_id": task_id})
                        if (
                            not row.is_error
                            and (row.structured_content or {}).get("status") in terminal
                        ):
                            confirmed = True
                            break
                        time.sleep(0.5)
            except Exception:
                pass
            unverified += int(not confirmed)
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
        assert not containers.stdout.strip(), "test containers remain after teardown"
        assert not networks.stdout.strip(), "test networks remain after teardown"
        assert unverified == 0, "submitted task terminal state not verified before teardown"
        assert bridge is None or bridge.poll() is not None
        final_fixtures = fixture_state()
        print("linux_l1_final_fixture_state=" + json.dumps(final_fixtures, sort_keys=True))
        cleanup_allowed = all(all(item.values()) for item in final_fixtures.values())
        assert cleanup_allowed, "unexpected workdir mutation; fixture retained"
        print("linux_l1_teardown=PASS containers=0 networks=0 bridge_stopped=true")

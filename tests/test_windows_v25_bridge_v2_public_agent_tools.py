"""One public MCP v1-shaped Agent surface over a fully scoped Bridge v2 client.

Exercises real MCP tool registration and v2 request signing using an in-memory
transport fake. Never launches provider, network, Windows Job, task or service.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from helpers import call_success, registry_for
from serverfs_mcp import agent_tools
from serverfs_mcp.agent_client import AgentBridgeRemoteError
from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import Workdir

_ENDPOINT = (
    Path(r"\\.\pipe\serverfs-v2-agent-tools-test")
    if sys.platform == "win32"
    else Path("/tmp/serverfs-v2-agent-tools-test")
)


def _app(tmp_path, monkeypatch):
    # Exercise Core↔Bridge signing rather than the separately packaged
    # native Windows filesystem backend absent in the source-only venv.
    monkeypatch.setattr(agent_tools, "_validate_agent_cwd", lambda wd, path, settings: path)
    sources = []
    for alias in ("RepoA", "RepoB"):
        root = tmp_path / alias
        root.mkdir()
        sources.append(
            Workdir(
                alias=alias,
                root=root,
                description=alias,
                read_only=False,
                agent_mode="workspace-write",
                agent_runtimes=frozenset({"codex", "claude"}),
            )
        )
    client = CoreGrantBridgeClient(
        _ENDPOINT,
        core_id="core-a",
        bridge_id="shared",
        key_id="key-a",
        secret=b"K" * 32,
        policy_revisions={"RepoA": "a" * 64, "RepoB": "b" * 64},
        server_pid_validator=(lambda pid: pid > 0) if sys.platform == "win32" else None,
    )
    captured = []

    async def exchange(raw, request_id):
        request = json.loads(raw)
        captured.append(request)
        assert request["protocol_version"] == 2
        assert request["request_id"] == request_id
        assert request["params"]["workdir"] in ("RepoA", "RepoB")
        method = request["method"]
        alias = request["params"]["workdir"]
        if method == "runtime.list":
            return {
                "runtimes": [{"name": "codex" if alias == "RepoA" else "claude", "available": True}]
            }
        if method == "runtime.models":
            if alias != "RepoB":
                raise AgentBridgeRemoteError(
                    "CORE_NOT_AUTHORIZED", "Core capability is not authorized"
                )
            return {
                "runtime": request["params"]["runtime"],
                "models": [{"id": "example-model"}],
                "status": "ok",
            }
        if method == "task.submit":
            if alias != "RepoB":
                raise AgentBridgeRemoteError(
                    "CORE_NOT_AUTHORIZED", "Core capability is not authorized"
                )
            return {"task_id": "agt_owned", "status": "queued"}
        if method in ("task.get", "task.cancel"):
            if alias != "RepoB":
                raise AgentBridgeRemoteError("CORE_TASK_NOT_FOUND", "Core task is not available")
            return {"task_id": "agt_owned", "status": "running"}
        raise AssertionError("unexpected bridge RPC: " + method)

    client._exchange = exchange
    app = create_server(
        Settings(
            agent_bridge_enabled=True,
            agent_bridge_socket=str(_ENDPOINT),
            agent_lock_dir=str(tmp_path / "locks"),
        ),
        registry_for(*sources),
        client,
    )
    return app, captured


def test_public_agent_tools_keep_legacy_arguments_over_v2_bridge(tmp_path, monkeypatch):
    app, calls = _app(tmp_path, monkeypatch)
    catalog = call_success(app, "list_agent_runtimes", {})
    assert [entry["name"] for entry in catalog["runtimes"]] == ["codex", "claude"]
    models = call_success(app, "list_agent_models", {"runtime": "claude"})
    assert models["models"][0]["id"] == "example-model"

    submitted = call_success(
        app,
        "submit_agent_task",
        {"runtime": "claude", "workdir": "RepoB", "prompt": "Review the fake repository"},
    )
    assert submitted["task_id"] == "agt_owned"

    state = call_success(app, "get_agent_task", {"task_id": "agt_owned"})
    assert state["task_id"] == "agt_owned"
    cancelled = call_success(app, "cancel_agent_task", {"task_id": "agt_owned"})
    assert cancelled["task_id"] == "agt_owned"

    expected = [
        "runtime.list",
        "runtime.list",
        "runtime.models",
        "runtime.models",
        "task.submit",
        "task.get",
        "task.get",
        "task.cancel",
        "task.cancel",
    ]
    assert [call["method"] for call in calls] == expected
    assert all("workdir" in call["params"] for call in calls)
    assert all(call["core_id"] == "core-a" for call in calls)
    assert all(call["bridge_id"] == "shared" for call in calls)
    assert len({call["nonce"] for call in calls}) == len(calls)

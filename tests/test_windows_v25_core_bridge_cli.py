"""Native Windows CLI accepts only an already validated v2 signed client.

The HTTP server and all Bridge transport exchanges are faked. No listener,
Scheduled Task, provider, network or installed private state is touched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import cli, config, main
from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient
from serverfs_mcp.config import Settings
from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, Workdir


def _inputs(tmp_path: Path, monkeypatch, *, needs_bridge=True):
    workdirs = [
        Workdir(
            alias="Repo",
            root=tmp_path,
            description=None,
            read_only=False,
            policy=EffectiveWorkdirPolicy(
                agent_mode="workspace-write", agent_runtimes=frozenset({"codex"})
            ),
        )
    ]
    monkeypatch.setattr(
        cli,
        "_load",
        lambda _: (workdirs, SimpleNamespace(bridge_required=needs_bridge, log_level="INFO")),
    )
    monkeypatch.setattr(
        config,
        "settings_from_env",
        lambda *_args, **_kwargs: Settings(
            http_api_key="synthetic-key",
            http_allowed_hosts=("127.0.0.1:18997", "core.example.test"),
            http_allowed_origins=("http://127.0.0.1:18997",),
        ),
    )
    monkeypatch.setattr(cli, "_overlay_ingress_env", lambda settings: settings)
    captured = []

    def fake_server(settings, registry, agent_client, ingress):
        captured.append(("server", settings, agent_client))
        return object()

    def fake_http(mcp, settings, registry):
        captured.append(("http", settings))

    monkeypatch.setattr(main, "create_server", fake_server)
    monkeypatch.setattr(main, "run_streamable_http", fake_http)
    args = cli.build_parser().parse_args(
        [
            "serve",
            "--config",
            str(tmp_path / "serverfs.toml"),
            "--transport",
            "streamable-http",
            "--port",
            "18997",
        ]
    )
    return args, captured


def _client():
    return CoreGrantBridgeClient(
        Path(r"\\.\pipe\serverfs-agent-bridge-v25-fake"),
        core_id="core-a",
        bridge_id="bridge-shared",
        key_id="key-a",
        secret=b"A" * 32,
        policy_revisions={"Repo": "a" * 64},
        server_pid_validator=lambda pid: pid > 0,
    )


@pytest.mark.skipif(
    sys.platform != "win32", reason="requires native Windows Named Pipe path semantics"
)
def test_native_core_injected_v2_client_is_used_for_mcp_tools(tmp_path, monkeypatch):
    args, captured = _inputs(tmp_path, monkeypatch)
    client = _client()
    settings = Settings(
        agent_bridge_enabled=True,
        agent_bridge_socket=str(client.socket_path),
        agent_lock_dir=str(tmp_path / "bridge-locks"),
    )
    assert cli.cmd_serve(args, agent_wiring_override=(settings, client)) == 0
    assert len(captured) == 2
    assert captured[0][0] == "server"
    assert captured[0][2] is client
    assert captured[0][1].agent_bridge_enabled
    assert captured[0][1].agent_lock_dir == settings.agent_lock_dir
    assert captured[1][0] == "http"


def test_invalid_v2_client_does_not_fallback_to_legacy_bridge(tmp_path, monkeypatch):
    args, captured = _inputs(tmp_path, monkeypatch)
    settings = Settings(agent_bridge_enabled=True)
    assert cli.cmd_serve(args, agent_wiring_override=(settings, object())) == 2
    assert not captured


@pytest.mark.skipif(
    sys.platform != "win32", reason="requires native Windows Named Pipe path semantics"
)
def test_file_only_core_cannot_be_tricked_into_agent_wiring(tmp_path, monkeypatch):
    args, captured = _inputs(tmp_path, monkeypatch, needs_bridge=False)
    assert (
        cli.cmd_serve(args, agent_wiring_override=(Settings(agent_bridge_enabled=True), _client()))
        == 2
    )
    assert not captured

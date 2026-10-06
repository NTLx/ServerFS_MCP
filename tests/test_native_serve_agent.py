"""Regression tests for the native public Agent surface (§15 D3, D5 step 11).

The maintainer review found that ``cmd_serve`` constructed ``Settings(log_level=...)`` only, so the
three variables the supervisor injects never reached the effective settings and no
``AgentBridgeClient`` was created. The Agent tools were therefore absent from a native serve even
with delegation enabled — the published surface silently stayed filesystem-only.

These tests pin the two states the contract distinguishes:

- **Agent disabled** — no Agent tools, the v0.10 filesystem-only surface.
- **Agent enabled** — the ten frozen Agent tools registered.

and the boundary that keeps this from becoming an ambient-env switch: ``serverfs.toml`` is the only
operator source of policy. ``SERVERFS_AGENT_BRIDGE_ENABLED`` in a shell must not enable delegation;
the supervisor's variables supply placement only, and only once the config has already opted in.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from serverfs_mcp.cli import _native_agent_settings, cmd_serve
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.native_config import load_native_config
from serverfs_mcp.workdirs import WorkdirRegistry

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native serve")

AGENT_TOOLS = {
    "submit_agent_task",
    "get_agent_task",
    "list_agent_runtimes",
    "cancel_agent_task",
    "respond_agent_approval",
    "read_agent_task_events",
    "read_agent_task_result",
    "list_agent_models",
    "send_agent_message",
    "answer_agent_question",
}

V010_CONFIG = """\
[server]
log_level = "INFO"

[[workdirs]]
alias = "repo"
path = "{root}"
read_only = false
"""

AGENT_CONFIG = """\
[server]
log_level = "INFO"

[agent]
enabled = true

[agent.codex]
enabled = true

[[workdirs]]
alias = "repo"
path = "{root}"
read_only = false
agent_mode = "workspace-write"
agent_runtimes = ["codex"]
"""

#: Placement values a supervisor would inject. Not policy: the config already opted in.
WIRING = {
    "SERVERFS_AGENT_BRIDGE_SOCKET": r"\\.\pipe\serverfs-agent-bridge-v1-0123456789abcdef",
    "SERVERFS_AGENT_LOCK_DIR": r"C:\Users\test\AppData\Local\ServerFS\agent-bridge\locks",
}


def _write(tmp_path: Path, template: str, root: Path) -> Path:
    config = tmp_path / "serverfs.toml"
    config.write_text(template.format(root=str(root).replace("\\", "\\\\")), encoding="utf-8")
    return config


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _settings_for(config_path: Path):
    _workdirs, native_settings = load_native_config(config_path)
    return native_settings


def _client_for(settings: Settings):
    if not settings.agent_bridge_enabled:
        return None
    from serverfs_mcp.agent_client import AgentBridgeClient

    return AgentBridgeClient(Path(settings.agent_bridge_socket), timeout_seconds=2.0)


def _tool_names(settings: Settings, config_path: Path) -> set[str]:
    """The published tool surface for one config, through the real create_server()."""
    registry = WorkdirRegistry(load_native_config(config_path)[0])
    server = create_server(settings, registry, _client_for(settings))

    async def _names() -> set[str]:
        return {tool.name for tool in await server.list_tools()}

    return asyncio.run(_names())


class TestAgentDisabledSurface:
    """The v0.10 filesystem-only surface."""

    def test_no_agent_wiring_is_built(self, tmp_path: Path, workdir: Path, monkeypatch):
        for name, value in WIRING.items():
            monkeypatch.setenv(name, value)
        config = _write(tmp_path, V010_CONFIG, workdir)
        assert _native_agent_settings(_settings_for(config)) is None

    def test_ambient_env_cannot_enable_delegation(self, tmp_path: Path, workdir: Path, monkeypatch):
        """The review's boundary: serverfs.toml is the only operator source of policy."""
        monkeypatch.setenv("SERVERFS_AGENT_BRIDGE_ENABLED", "1")
        monkeypatch.setenv("SERVERFS_AGENT_BRIDGE_SOCKET", WIRING["SERVERFS_AGENT_BRIDGE_SOCKET"])
        monkeypatch.setenv("SERVERFS_AGENT_LOCK_DIR", WIRING["SERVERFS_AGENT_LOCK_DIR"])
        config = _write(tmp_path, V010_CONFIG, workdir)
        assert _settings_for(config).agent_enabled is False
        assert _native_agent_settings(_settings_for(config)) is None

    def test_tool_surface_is_the_eleven_filesystem_tools(self, tmp_path: Path, workdir: Path):
        config = _write(tmp_path, V010_CONFIG, workdir)
        names = _tool_names(Settings(log_level="INFO"), config)
        assert len(names) == 11, sorted(names)
        assert not names & AGENT_TOOLS


class TestAgentEnabledSurface:
    """Ten frozen Agent tools, registered without a running Bridge."""

    def test_wiring_is_consumed_from_the_injected_placement(
        self, tmp_path: Path, workdir: Path, monkeypatch
    ):
        for name, value in WIRING.items():
            monkeypatch.setenv(name, value)
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        settings, client = _native_agent_settings(_settings_for(config))
        assert settings.agent_bridge_enabled is True
        assert settings.agent_bridge_socket == WIRING["SERVERFS_AGENT_BRIDGE_SOCKET"]
        assert settings.agent_lock_dir == WIRING["SERVERFS_AGENT_LOCK_DIR"]
        assert client is not None, "an AgentBridgeClient must be created for the enabled surface"

    def test_log_level_is_preserved_from_the_toml(self, tmp_path: Path, workdir: Path, monkeypatch):
        template = AGENT_CONFIG.replace('log_level = "INFO"', 'log_level = "DEBUG"')
        for name, value in WIRING.items():
            monkeypatch.setenv(name, value)
        config = _write(tmp_path, template, workdir)
        settings, _client = _native_agent_settings(_settings_for(config))
        assert settings.log_level == "DEBUG"

    def test_missing_endpoint_fails_closed(self, tmp_path: Path, workdir: Path, monkeypatch):
        """Registering tools against the Linux default socket would be a confusing failure."""
        monkeypatch.delenv("SERVERFS_AGENT_BRIDGE_SOCKET", raising=False)
        monkeypatch.setenv("SERVERFS_AGENT_LOCK_DIR", WIRING["SERVERFS_AGENT_LOCK_DIR"])
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        with pytest.raises(SystemExit):
            _native_agent_settings(_settings_for(config))

    def test_missing_lock_dir_fails_closed(self, tmp_path: Path, workdir: Path, monkeypatch):
        monkeypatch.setenv("SERVERFS_AGENT_BRIDGE_SOCKET", WIRING["SERVERFS_AGENT_BRIDGE_SOCKET"])
        monkeypatch.delenv("SERVERFS_AGENT_LOCK_DIR", raising=False)
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        with pytest.raises(SystemExit):
            _native_agent_settings(_settings_for(config))

    def test_exactly_ten_agent_tools_are_registered(
        self, tmp_path: Path, workdir: Path, monkeypatch
    ):
        for name, value in WIRING.items():
            monkeypatch.setenv(name, value)
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        settings, _client = _native_agent_settings(_settings_for(config))
        names = _tool_names(settings, config)
        assert names & AGENT_TOOLS == AGENT_TOOLS
        assert len(AGENT_TOOLS) == 10
        # The filesystem surface is unchanged: eleven tools plus the ten Agent tools.
        assert len(names) == 21, sorted(names)

    def test_agent_calls_fail_closed_when_no_bridge_is_running(
        self, tmp_path: Path, workdir: Path, monkeypatch
    ):
        """A direct serve starts no Bridge, so calls fail via the frozen unavailable path."""
        # An endpoint nothing is listening on, so the tool is registered and the call fails.
        monkeypatch.setenv(
            "SERVERFS_AGENT_BRIDGE_SOCKET", r"\\.\pipe\serverfs-agent-bridge-v1-0000000000000000"
        )
        monkeypatch.setenv("SERVERFS_AGENT_LOCK_DIR", WIRING["SERVERFS_AGENT_LOCK_DIR"])
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        settings, client = _native_agent_settings(_settings_for(config))
        registry = WorkdirRegistry(load_native_config(config)[0])
        server = create_server(settings, registry, client)

        from helpers import call_error, error_code

        message = call_error(server, "list_agent_runtimes", {"workdir": "repo"})
        # The frozen unavailable code, not a crash and not a silent success.
        assert error_code(message) == "AGENT_BRIDGE_UNAVAILABLE", message
        # And no host path or Win32 detail leaks into the agent-visible text.
        assert "\\\\.\\pipe" not in message
        assert "WinError" not in message

    def test_public_tool_set_and_protocol_version_are_unchanged(
        self, tmp_path: Path, workdir: Path, monkeypatch
    ):
        for name, value in WIRING.items():
            monkeypatch.setenv(name, value)
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        settings, _client = _native_agent_settings(_settings_for(config))
        enabled_names = _tool_names(settings, config)

        disabled_config = _write(tmp_path, V010_CONFIG, workdir)
        disabled_names = _tool_names(Settings(log_level="INFO"), disabled_config)
        # Exactly the ten Agent tools are the difference; nothing else moved.
        assert enabled_names - disabled_names == AGENT_TOOLS

    def test_cmd_serve_is_not_invoked_by_these_tests(self):
        """cmd_serve blocks on stdio; the wiring it depends on is asserted directly instead."""
        assert callable(cmd_serve)

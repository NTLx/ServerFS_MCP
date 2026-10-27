"""Native TOML Repo Ops policy and macOS Bridge-required regressions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.agent_lifecycle import render_request_from_config
from serverfs_mcp.config import Settings
from serverfs_mcp.darwin_bridge_policy import _private_overlay
from serverfs_mcp.main import create_server
from serverfs_mcp.native_config import NativeConfigError, load_native_config
from serverfs_mcp.workdirs import WorkdirRegistry

PIN = "a" * 64


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "serverfs.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _repo_only(root: Path, *, action: bool = True) -> str:
    action_table = (
        """
[workdirs.repo_actions.wiki_lint]
argv = ["uv", "run", "--frozen", "python", "tools/lint.py"]
mutates = false
argument = "none"

[workdirs.repo_actions.wiki_lint.integrity]
"tools/lint.py" = """
        + f'"{PIN}"'
        + "\n"
    )
    return (
        f'[[workdirs]]\nalias = "repo"\npath = "{root}"\nread_only = false\n'
        "repo_ops_enabled = true\n" + (action_table if action else "")
    )


def test_old_native_config_defaults_repo_ops_off_and_bridge_not_required(tmp_path: Path) -> None:
    workdirs, settings = load_native_config(
        _config(tmp_path, '[[workdirs]]\nalias = "repo"\npath = "/srv/repo"\n')
    )
    assert workdirs[0].policy.repo_ops_enabled is False
    assert settings.repo_ops_enabled is False
    assert settings.bridge_required is False


def test_repo_only_toml_parses_and_renders_fixed_action(tmp_path: Path) -> None:
    workdirs, settings = load_native_config(_config(tmp_path, _repo_only(tmp_path / "repo")))
    request = render_request_from_config(workdirs, settings)
    assert settings.bridge_required is True
    assert workdirs[0].policy.repo_ops_enabled is True
    assert request == {
        "workdirs": [
            {
                "alias": "repo",
                "host_path": str(tmp_path / "repo"),
                "read_only": False,
                "agent_mode": "disabled",
                "agent_runtimes": [],
                "repo_ops_enabled": True,
                "repo_actions": {
                    "wiki_lint": {
                        "argv": ["uv", "run", "--frozen", "python", "tools/lint.py"],
                        "mutates": False,
                        "argument": "none",
                        "integrity": {"tools/lint.py": PIN},
                    }
                },
            }
        ]
    }
    assert "runtimes" not in request and "limits" not in request


def test_shell_looking_argv_remains_a_literal_fixed_argument(tmp_path: Path) -> None:
    body = _repo_only(tmp_path / "repo").replace('"tools/lint.py"]', '"$(id)"]')
    workdirs, settings = load_native_config(_config(tmp_path, body))
    request = render_request_from_config(workdirs, settings)
    assert request["workdirs"][0]["repo_actions"]["wiki_lint"]["argv"][-1] == "$(id)"


def test_repo_only_native_policy_exposes_only_the_four_repo_tools(tmp_path: Path) -> None:
    config = _config(tmp_path, _repo_only(tmp_path / "repo"))
    workdirs, _settings = load_native_config(config)
    server = create_server(
        Settings(
            agent_bridge_enabled=True,
            agent_bridge_socket="/tmp/serverfs-bridge.sock",
            agent_lock_dir="/tmp/serverfs-locks",
        ),
        WorkdirRegistry(workdirs),
        object(),
    )
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {"inspect_repo", "run_repo_action", "commit_repo_changes", "push_repo"} <= names
    assert "submit_agent_task" not in names


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            _repo_only(Path("/srv/repo")).replace(
                "repo_ops_enabled = true", 'repo_ops_enabled = "true"'
            ),
            "repo_ops_enabled",
        ),
        (_repo_only(Path("/srv/repo")).replace("mutates = false", 'mutates = "false"'), "mutates"),
        (_repo_only(Path("/srv/repo")).replace(f'"{PIN}"', '"bad"'), "sha256"),
        (
            _repo_only(Path("/srv/repo")).replace('"tools/lint.py"]', '"bad\\u0000arg"]'),
            "bounded strings",
        ),
        (
            _repo_only(Path("/srv/repo")).replace('"tools/lint.py"]', '"bad\\narg"]'),
            "bounded strings",
        ),
        (
            _repo_only(Path("/srv/repo")).replace('"tools/lint.py"]', f'"{"x" * 4097}"]'),
            "bounded strings",
        ),
    ],
)
def test_repo_action_policy_is_strict_at_startup(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(NativeConfigError, match=message):
        load_native_config(_config(tmp_path, body))


def test_read_only_repo_workdir_cannot_configure_named_actions(tmp_path: Path) -> None:
    body = _repo_only(tmp_path / "repo").replace("read_only = false", "read_only = true")
    with pytest.raises(NativeConfigError, match="read-only workdir"):
        load_native_config(_config(tmp_path, body))


def test_named_actions_require_repo_ops_enable(tmp_path: Path) -> None:
    body = _repo_only(tmp_path / "repo").replace("repo_ops_enabled = true\n", "")
    with pytest.raises(NativeConfigError, match="requires repo_ops_enabled"):
        load_native_config(_config(tmp_path, body))


def test_mixed_agent_and_repo_ops_renders_both_policy_domains(tmp_path: Path) -> None:
    body = "[agent]\nenabled = true\n\n[agent.codex]\nenabled = true\n\n" + _repo_only(
        tmp_path / "repo"
    ).replace(
        "repo_ops_enabled = true\n",
        'repo_ops_enabled = true\nagent_mode = "workspace-write"\nagent_runtimes = ["codex"]\n',
    )
    workdirs, settings = load_native_config(_config(tmp_path, body))
    request = render_request_from_config(workdirs, settings)
    assert settings.bridge_required is True
    assert request["runtimes"]["codex"]["enabled"] is True
    assert request["workdirs"][0]["agent_runtimes"] == ["codex"]
    assert request["workdirs"][0]["repo_actions"]["wiki_lint"]["integrity"] == {
        "tools/lint.py": PIN
    }


def test_repo_only_private_overlay_needs_no_proxy_or_jev_material() -> None:
    private = _private_overlay(SimpleNamespace(agent=None), {})
    assert private == {}


def test_macos_configure_accepts_repo_only_native_policy(monkeypatch, tmp_path: Path) -> None:
    from serverfs_mcp import cli, darwin_bridge_policy

    root = tmp_path / "repo"
    root.mkdir()
    config = _config(tmp_path, _repo_only(root))
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    calls: list[bool] = []
    monkeypatch.setattr(
        darwin_bridge_policy,
        "configure_policy",
        lambda _workdirs, settings, **_kwargs: calls.append(settings.bridge_required),
    )
    result = cli.cmd_agent_bridge(
        SimpleNamespace(agent_command="configure", config=config, env_file=None)
    )
    assert result == 0
    assert calls == [True]


def test_macos_install_configures_and_registers_bridge_for_repo_only(
    monkeypatch, tmp_path: Path
) -> None:
    from serverfs_mcp import cli, darwin_bridge_policy, darwin_service

    root = tmp_path / "repo"
    root.mkdir()
    config = _config(tmp_path, _repo_only(root))
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr(darwin_service, "find_unmanaged_tunnel_roots", lambda: [])
    monkeypatch.setattr(
        darwin_service, "legacy_bridge_plist_path", lambda: tmp_path / "missing.plist"
    )
    configured: list[bool] = []
    installed: list[bool] = []
    monkeypatch.setattr(
        darwin_bridge_policy,
        "configure_policy",
        lambda _workdirs, settings, **_kwargs: configured.append(settings.bridge_required),
    )
    monkeypatch.setattr(
        darwin_service,
        "install",
        lambda *_args, bridge_enabled, **_kwargs: (
            installed.append(bridge_enabled) or {"tunnel": "enabled", "agent-bridge": "enabled"}
        ),
    )
    result = cli.cmd_service(
        SimpleNamespace(service_command="install", config=config, env_file=env_file)
    )
    assert result == 0
    assert configured == [True]
    assert installed == [True]

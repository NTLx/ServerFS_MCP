"""v0.13.2 compatibility tests for the retired direct macOS LaunchAgent path."""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from serverfs_mcp import cli
from serverfs_mcp.darwin_lifecycle import (
    AGENT_LABEL,
    installed_bridge_config_path,
    launch_agent_plist_path,
)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin lifecycle contract")


class TestLegacyLaunchAgentInspection:
    def test_legacy_plist_path_uses_the_frozen_label(self) -> None:
        assert launch_agent_plist_path().name == f"{AGENT_LABEL}.plist"
        assert launch_agent_plist_path().parent.name == "LaunchAgents"

    def test_installed_bridge_config_path_comes_from_legacy_plist(
        self, tmp_path, monkeypatch
    ) -> None:
        from serverfs_mcp import darwin_lifecycle

        plist_path = tmp_path / "agent.plist"
        private_config = tmp_path / "private" / "bridge.json"
        plist_path.write_bytes(
            plistlib.dumps(
                {
                    "ProgramArguments": [
                        "/opt/serverfs-agent-bridge",
                        "--config",
                        str(private_config),
                    ]
                }
            )
        )
        monkeypatch.setattr(darwin_lifecycle, "launch_agent_plist_path", lambda: plist_path)

        assert installed_bridge_config_path() == private_config

    def test_installed_bridge_config_path_refuses_malformed_plist(
        self, tmp_path, monkeypatch
    ) -> None:
        from serverfs_mcp import darwin_lifecycle

        plist_path = tmp_path / "agent.plist"
        plist_path.write_bytes(plistlib.dumps({"ProgramArguments": ["bridge", "--wrong", "/x"]}))
        monkeypatch.setattr(darwin_lifecycle, "launch_agent_plist_path", lambda: plist_path)

        assert installed_bridge_config_path() is None


class TestCliCompatibility:
    def test_legacy_install_directs_operator_to_service_manager(self, tmp_path, capsys) -> None:
        code = cli.main(
            [
                "agent-bridge",
                "install",
                "--bridge-config",
                str(tmp_path / "bridge.json"),
            ]
        )
        assert code == 2
        assert "v0.13.2 no longer installs" in capsys.readouterr().err

    def test_status_reports_native_service_state(self, capsys, monkeypatch) -> None:
        from serverfs_mcp import darwin_service

        monkeypatch.setattr(
            darwin_service,
            "status",
            lambda: {
                "services": {"agent-bridge": "not-registered"},
                "processes": {"agent-bridge": {"pid": None}},
            },
        )
        code = cli.main(["agent-bridge", "status"])
        assert code == 0
        output = capsys.readouterr().err
        assert "state: not-registered" in output
        assert "pid:" in output


def _write_agent_config(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    config = tmp_path / "serverfs.toml"
    config.write_text(
        f"""[agent]\nenabled = true\n\n[agent.codex]\nenabled = true\nuse_proxy = false\n\n"""
        f'''[[workdirs]]\nalias = "repo"\npath = "{root}"\nread_only = false\n'''
        'agent_mode = "workspace-write"\nagent_runtimes = ["codex"]\n',
        encoding="utf-8",
    )
    return config


class TestBridgePolicyLifecycle:
    def test_restart_with_config_syncs_before_native_service_restart(
        self, tmp_path, monkeypatch
    ) -> None:
        from serverfs_mcp import darwin_bridge_policy, darwin_service

        config = _write_agent_config(tmp_path)
        calls: list[str] = []
        monkeypatch.setattr(
            darwin_bridge_policy,
            "sync_policy",
            lambda _workdirs, _settings: calls.append("sync"),
        )
        monkeypatch.setattr(
            darwin_service,
            "restart_service",
            lambda _service: calls.append("restart"),
        )

        code = cli.main(["agent-bridge", "restart", "--config", str(config)])

        assert code == 0
        assert calls == ["sync", "restart"]

    def test_restart_with_env_file_configures_private_state_before_restart(
        self, tmp_path, monkeypatch
    ) -> None:
        from serverfs_mcp import darwin_bridge_policy, darwin_service

        config = _write_agent_config(tmp_path)
        env_file = tmp_path / ".env"
        env_file.write_text("", encoding="utf-8")
        calls: list[tuple[str, Path | None]] = []

        def configure(_workdirs, _settings, *, env_file: Path | None = None) -> None:
            calls.append(("configure", env_file))

        monkeypatch.setattr(darwin_bridge_policy, "configure_policy", configure)
        monkeypatch.setattr(
            darwin_service,
            "restart_service",
            lambda _service: calls.append(("restart", None)),
        )

        code = cli.main(
            [
                "agent-bridge",
                "restart",
                "--config",
                str(config),
                "--env-file",
                str(env_file),
            ]
        )

        assert code == 0
        assert calls == [("configure", env_file), ("restart", None)]

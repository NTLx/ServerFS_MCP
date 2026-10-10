"""macOS LaunchAgent lifecycle tests (v0.13 Phase E).

Pure logic (plist rendering, executable discovery, CLI refusals) runs on
every darwin host. The live launchd test bootstraps a REAL per-user
LaunchAgent running the REAL bridge executable against a fake-runtime
config, waits for the AF_UNIX endpoint, talks to it through the real
client, restarts and removes it. It is opt-in via
``SERVERFS_LIVE_LAUNCHD=1`` so hosted CI runs only the pure logic;
the live proof is part of local/acceptance runs (dev_plan_v0.13.md §12).
"""

from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import time
from pathlib import Path

import pytest

from serverfs_mcp import cli
from serverfs_mcp.darwin_lifecycle import (
    AGENT_LABEL,
    EXIT_TIMEOUT_SECONDS,
    LaunchAgentPlan,
    default_bridge_executable,
    installed_bridge_config_path,
    launch_agent_plist_path,
)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin lifecycle contract")


class TestPlistGeneration:
    def test_renders_paths_only_with_expected_keys(self, tmp_path) -> None:
        plan = LaunchAgentPlan(
            bridge_executable=tmp_path / "serverfs-agent-bridge",
            bridge_config_path=tmp_path / "bridge.json",
            log_dir=tmp_path / "logs",
        )
        data = plistlib.loads(plan.render())
        assert data["Label"] == AGENT_LABEL
        assert data["ProgramArguments"] == [
            str(tmp_path / "serverfs-agent-bridge"),
            "--config",
            str(tmp_path / "bridge.json"),
        ]
        assert data["RunAtLoad"] is True
        assert data["KeepAlive"] is True
        assert data["ExitTimeOut"] == EXIT_TIMEOUT_SECONDS
        assert data["StandardErrorPath"].endswith("bridge.stderr.log")
        rendered = plan.render().decode()
        # the plist carries paths, never secret material
        assert "password" not in rendered.lower()
        assert "token" not in rendered.lower()

    def test_plist_path_uses_the_frozen_label(self) -> None:
        assert launch_agent_plist_path().name == f"{AGENT_LABEL}.plist"
        assert launch_agent_plist_path().parent.name == "LaunchAgents"

    def test_installed_bridge_config_path_comes_from_launchagent_plist(
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

    def test_default_bridge_executable_discovery(self) -> None:
        # the development venv runs 'uv sync --project agent_bridge', so the
        # console script exists beside this interpreter's directory sibling
        try:
            exe = default_bridge_executable()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"bridge console script not installed: {exc}")
        assert exe.is_file() and os.access(exe, os.X_OK)


class TestCliRefusals:
    def test_install_refuses_missing_bridge_config(self, tmp_path, capsys) -> None:
        fake_exe = tmp_path / "serverfs-agent-bridge"
        fake_exe.write_text("#!/bin/sh\n")
        fake_exe.chmod(0o755)
        code = cli.main(
            [
                "agent-bridge",
                "install",
                "--bridge-executable",
                str(fake_exe),
                "--bridge-config",
                str(tmp_path / "absent.json"),
            ]
        )
        assert code == 2
        assert "bridge configuration file not found" in capsys.readouterr().err

    def test_status_reports_not_bootstrapped(self, capsys) -> None:
        # read-only: safe to run anywhere; the agent is not installed here
        from serverfs_mcp import darwin_lifecycle

        if darwin_lifecycle.launch_agent_plist_path().exists():
            pytest.skip("agent installed on this host; status reflects the real one")
        code = cli.main(["agent-bridge", "status"])
        assert code == 0
        assert "not-bootstrapped" in capsys.readouterr().err


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
    def test_restart_with_config_syncs_before_launchd_restart(self, tmp_path, monkeypatch) -> None:
        from serverfs_mcp import darwin_bridge_policy, darwin_lifecycle

        config = _write_agent_config(tmp_path)
        calls: list[str] = []
        monkeypatch.setattr(
            darwin_bridge_policy,
            "sync_policy",
            lambda _workdirs, _settings: calls.append("sync"),
        )
        monkeypatch.setattr(darwin_lifecycle, "restart", lambda: calls.append("restart"))

        code = cli.main(["agent-bridge", "restart", "--config", str(config)])

        assert code == 0
        assert calls == ["sync", "restart"]

    def test_restart_with_env_file_configures_private_state_before_restart(
        self, tmp_path, monkeypatch
    ) -> None:
        from serverfs_mcp import darwin_bridge_policy, darwin_lifecycle

        config = _write_agent_config(tmp_path)
        env_file = tmp_path / ".env"
        env_file.write_text("", encoding="utf-8")
        calls: list[tuple[str, Path | None]] = []

        def configure(_workdirs, _settings, *, env_file: Path | None = None) -> None:
            calls.append(("configure", env_file))

        monkeypatch.setattr(darwin_bridge_policy, "configure_policy", configure)
        monkeypatch.setattr(
            darwin_lifecycle,
            "restart",
            lambda: calls.append(("restart", None)),
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


@pytest.mark.skipif(
    os.environ.get("SERVERFS_LIVE_LAUNCHD") != "1",
    reason="live launchd proof is opt-in (SERVERFS_LIVE_LAUNCHD=1)",
)
class TestLiveLaunchd:
    """Real bootstrap/kickstart/bootout of the real bridge under launchd."""

    @pytest.fixture()
    def bridge_setup(self, tmp_path_factory):
        bridge_python = Path(
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "agent_bridge", ".venv"))
        )
        exe = bridge_python / "bin" / "serverfs-agent-bridge"
        if not exe.is_file():
            pytest.skip("bridge console script not installed")
        # the socket path must fit the sun_path budget: use a short root
        root = Path(tempfile.mkdtemp(prefix="sfs-e2e-"))
        runtime = root / "run"
        runtime.mkdir()
        config = {
            "socket_path": str(runtime / "bridge.sock"),
            "state_dir": str(root / "state"),
            "lock_dir": str(root / "locks"),
            "enable_fake_runtime": True,
            "workdirs": [
                {
                    "slot": 1,
                    "alias": "scratch",
                    "host_path": str(root / "workdir"),
                    "read_only": False,
                    "agent_mode": "workspace-write",
                    "agent_runtimes": ["fake"],
                }
            ],
        }
        (root / "workdir").mkdir()
        config_path = root / "bridge.json"
        config_path.write_text(json.dumps(config))
        return exe, config_path, runtime / "bridge.sock", root

    def _wait_for_socket(self, path: Path, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.2)
        raise AssertionError(f"bridge socket never appeared at {path}")

    def _runtime_list(self, socket_path: Path) -> dict:
        import asyncio

        from serverfs_mcp.agent_client import AgentBridgeClient

        async def call() -> dict:
            client = AgentBridgeClient(socket_path, timeout_seconds=15.0)
            return await client.call("runtime.list", {})

        return asyncio.run(call())

    def test_bootstrap_kickstart_bootout(self, bridge_setup, monkeypatch) -> None:
        exe, config_path, socket_path, root = bridge_setup
        from serverfs_mcp import darwin_lifecycle

        monkeypatch.setattr(
            darwin_lifecycle, "launch_agent_plist_path", lambda: root / "agent.plist"
        )
        plan = LaunchAgentPlan(
            bridge_executable=exe,
            bridge_config_path=config_path,
            log_dir=root / "logs",
        )
        try:
            darwin_lifecycle.install(plan)
            self._wait_for_socket(socket_path)
            result = self._runtime_list(socket_path)
            assert "runtimes" in result

            # restart under the same label
            darwin_lifecycle.restart()
            self._wait_for_socket(socket_path)
            assert "runtimes" in self._runtime_list(socket_path)

            state = darwin_lifecycle.status()
            # launchd reports either running/active depending on job kind
            assert state["state"] in {"running", "active"}
            assert state.get("pid", "") != ""
        finally:
            darwin_lifecycle.uninstall()
        # bootout removed the direct process; the endpoint is gone
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and socket_path.exists():
            time.sleep(0.2)
        assert not socket_path.exists()
